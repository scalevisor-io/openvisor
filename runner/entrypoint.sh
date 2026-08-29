#!/bin/bash
# Openvisor dev runner entrypoint. Expects:
#   /workspace                  - project repo (bind/volume mount), cwd for the agent
#   /workspace/.openvisor/task.md - the assembled dev task (system prompt + context)
#   /workspace/.openvisor/mcp.json→ copied to ~/.openhands/mcp.json (Context7 + browser + connected KBs)
#   /workspace/.openvisor/deploy_key (optional) - per-project SSH key for git push
#   env: LLM_MODEL, LLM_API_KEY, LLM_BASE_URL, AGENT_BRANCH, GIT_PUSH=0|1
set -euo pipefail
# set -e failures used to die with stderr only, which the deployer's log capture
# dropped - name the dying command on stdout so the build panel shows it.
trap 'echo "runner: FAILED (exit $?) at line $LINENO: $BASH_COMMAND"' ERR

# Point $1 at commit-ish $2 and check it out WITHOUT clobbering the workspace:
# untracked files (the worker's fresh scaffold, partial build output) collide
# with `checkout -B` and used to abort the whole run. Falls back to moving the
# branch ref + resetting only the index, so the workspace content wins and
# `git add -A` later stages it as the diff against $2.
checkout_keeping_workspace() {
  if ! git -C /workspace checkout -q -B "$1" "$2" 2>/dev/null; then
    git -C /workspace update-ref "refs/heads/$1" "$(git -C /workspace rev-parse "$2")"
    git -C /workspace symbolic-ref HEAD "refs/heads/$1"
    git -C /workspace reset -q
    echo "runner: kept workspace files over $2 (untracked collision)"
  fi
}

OPENVISOR_DIR=/workspace/.openvisor
TASK_FILE="$OPENVISOR_DIR/task.md"

# Live build-panel feed (§14.8): entrypoint phase markers around the agent
# events run_dev.py emits. FIXED pre-composed strings only - no dynamic content
# is ever interpolated, so no JSON escaping (or secret) can sneak into a line.
emit_event() {
  # mkdir -p: the agent can delete .openvisor/ mid-run (`git clean -fdx` - it is
  # gitignored); recreate it so the feed and the billing artifact keep landing.
  mkdir -p "$OPENVISOR_DIR" 2>/dev/null || true
  printf '{"ts": %s, "kind": "%s", "title": "%s"}\n' "$(date +%s)" "$1" "$2" \
    >> "$OPENVISOR_DIR/events.jsonl" 2>/dev/null || true
}

if [[ ! -f "$TASK_FILE" ]]; then
  echo "runner: no task file at $TASK_FILE" >&2
  exit 2
fi

emit_event phase "Sandbox up - preparing the workspace"

# MCP servers for the agent (Context7 for live docs, the browser tool for checking the running app).
mkdir -p /root/.openhands
if [[ -f "$OPENVISOR_DIR/mcp.json" ]]; then
  cp "$OPENVISOR_DIR/mcp.json" /root/.openhands/mcp.json
fi

# Per-project deploy key so the agent can push its MR branch.
if [[ -f "$OPENVISOR_DIR/deploy_key" ]]; then
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  cp "$OPENVISOR_DIR/deploy_key" /root/.ssh/id_ed25519
  chmod 600 /root/.ssh/id_ed25519
  export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
fi

# Secret project-Memory entries, written by the worker (never passed as docker
# -e args, which would land in deployer logs / docker inspect). Exported to the
# agent's environment, then removed so they can't end up in the pushed repo.
if [[ -f "$OPENVISOR_DIR/secrets.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$OPENVISOR_DIR/secrets.env"
  set +a
  rm -f "$OPENVISOR_DIR/secrets.env"
  echo "runner: project Memory secrets exported to the environment"
fi

# §git identity: the platform sends the project's configured author (its own
# override, else the instance default); the literals are the standalone fallback
# for a runner started without them.
git config --global user.email "${GIT_USER_EMAIL:-agent@openvisor.example.com}" || true
git config --global user.name "${GIT_USER_NAME:-Openvisor agent}" || true
git config --global --add safe.directory /workspace || true

# §dev-docker: an inner Docker daemon for the agent (`docker compose` boots the
# project stack in-sandbox). Works because the sandbox runs under Sysbox in
# production - or --privileged in local dev - when DEV_SANDBOX_DOCKER=1 gates it
# on. Best-effort: a build must never fail because dockerd didn't come up.
if [[ "${DEV_DOCKER:-0}" == "1" ]]; then
  if ! docker info >/dev/null 2>&1; then
    echo "runner: starting inner dockerd (DEV_DOCKER=1)"
    dockerd > "$OPENVISOR_DIR/dockerd.log" 2>&1 &
    for _ in $(seq 1 25); do
      docker info >/dev/null 2>&1 && break
      sleep 1
    done
  fi
  if docker info >/dev/null 2>&1; then
    echo "runner: inner docker ready"
    emit_event phase "Docker-in-Docker ready"
  else
    echo "runner: WARNING - inner dockerd failed to start; building without docker"
    tail -n 5 "$OPENVISOR_DIR/dockerd.log" 2>/dev/null || true
    emit_event error "Docker unavailable in this sandbox"
  fi
fi

# gh reads GITHUB_TOKEN from the environment as-is (a project Memory secret when
# the customer set one). glab needs the self-hosted host spelled out.
# §glab api host: the worker passes GITLAB_HOST - the base /api/v4 actually
# answers on, resolved by the same `customer_base_url` the platform's own API
# calls use. Deriving it from the push remote is the FALLBACK, and it is only
# correct when an instance serves SSH and the API on one hostname: where they
# differ, every glab call leaves for the SSH name and lands on whatever else
# that address serves (observed live as a 502 carrying an unrelated host's TLS
# certificate, which reads like a network fault rather than a misrouted API).
if [[ -n "${GITLAB_HOST:-}" ]]; then
  echo "runner: GITLAB_HOST=$GITLAB_HOST (from the platform)"
elif [[ -n "${GITLAB_TOKEN:-}" && -n "${GIT_REMOTE_URL:-}" ]]; then
  _gl_host="${GIT_REMOTE_URL#*@}"; _gl_host="${_gl_host%%[:/]*}"
  if [[ -n "$_gl_host" ]]; then
    export GITLAB_HOST="$_gl_host"
    echo "runner: GITLAB_HOST=$_gl_host (derived from the remote - the platform sent none)"
  fi
fi

BRANCH="${AGENT_BRANCH:-agent/mvp}"
DEFAULT_BRANCH="${GIT_DEFAULT_BRANCH:-main}"

# Turn /workspace into a real checkout of the project's GitLab repo so the agent
# works on-repo and its branch can be pushed as an MR. The workspace already holds
# the .openvisor/ inputs, so we init-in-place + fetch rather than clone into it.
if [[ -n "${GIT_REMOTE_URL:-}" ]]; then
  if ! git -C /workspace rev-parse --git-dir >/dev/null 2>&1; then
    git -C /workspace init -q
  fi
  # The workspace persists across runs: re-point origin every run so a changed
  # push repo (§multi-repo push-target flip) takes effect - a stale origin pushes
  # the branch to the old repo and the worker's PR-open then 422s (head invalid).
  if git -C /workspace remote get-url origin >/dev/null 2>&1; then
    git -C /workspace remote set-url origin "$GIT_REMOTE_URL"
  else
    git -C /workspace remote add origin "$GIT_REMOTE_URL"
  fi
  # --prune: the workspace persists across runs, and a remote-tracking ref for a
  # branch the remote deleted (GitLab drops agent/mvp when its MR merges) makes
  # the bare --force-with-lease push below reject with "stale info".
  if ! git -C /workspace fetch -q --prune origin 2>/dev/null; then
    # §sandbox git preflight: ask the remote itself what this failure means.
    # ls-remote SUCCEEDS against an empty/new repository (nothing to fetch) and
    # fails when the sandbox cannot reach or authenticate to the host - the case
    # that must never become a build. The seeded workspace still holds usable
    # refs, so a severed path used to masquerade as a normal run for the whole
    # session and only surface at the final push: a full build's spend for work
    # that could never be published. Retried once - a fetch can lose a race.
    # ConnectTimeout: without it ssh sits on an unanswered SYN longer than the
    # 45 s ceiling, and the kill leaves NOTHING on stderr - the verdict is still
    # right but the console line that explains it comes out empty, which is the
    # difference between reading the failure and re-diagnosing it.
    REMOTE_ERR=""
    for _ in 1 2; do
      if REMOTE_ERR=$(GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh} -o ConnectTimeout=20" \
          timeout 45 git -C /workspace ls-remote origin 2>&1 >/dev/null); then
        REMOTE_ERR="reachable"
        break
      fi
    done
    if [[ "$REMOTE_ERR" == "reachable" ]]; then
      echo "runner: WARNING - fetch from origin failed but the remote answers (new/empty repository) - continuing without remote refs"
      emit_event error "Repository fetch failed - building without remote refs"
    elif [[ "${GIT_PUSH:-0}" != "1" ]]; then
      # A plan-only pass publishes nothing: it can still explore the seeded
      # workspace, and the build that follows runs this preflight for real.
      echo "runner: WARNING - remote unreachable; this run publishes nothing, continuing without remote refs"
      emit_event error "Repository fetch failed - building without remote refs"
    else
      # head, not tail: ssh reports the real cause on the FIRST line and git
      # closes with its generic "make sure you have the correct access rights"
      # advice - the line that reads like an auth problem when the host simply
      # never answered, and the one this incident wasted its first hour on.
      REMOTE_DETAIL=$(echo "$REMOTE_ERR" | head -n 2)
      echo "runner: remote error: ${REMOTE_DETAIL:-the git host answered nothing before the probe gave up}" >&2
      case "$REMOTE_ERR" in
        *"Permission denied"*|*"publickey"*|*"Authentication failed"*|*"not allowed"*)
          echo "runner: GIT_REMOTE_DENIED - the git host answered but refused this sandbox's key" >&2
          emit_event error "The repository refused the sandbox's key" ;;
        *)
          echo "runner: GIT_REMOTE_UNREACHABLE - no route from this sandbox to the git remote" >&2
          emit_event error "Cannot reach the code repository from this sandbox" ;;
      esac
      exit 6
    fi
  fi
  if git -C /workspace rev-parse --verify -q "origin/$BRANCH" >/dev/null && \
     { ! git -C /workspace rev-parse --verify -q "origin/$DEFAULT_BRANCH" >/dev/null || \
       git -C /workspace merge-base --is-ancestor "origin/$DEFAULT_BRANCH" "origin/$BRANCH"; }; then
    # retry: continue on top of the previous agent work so a CI fix builds on it
    # (only when it already contains the current base branch, so the PR/MR still
    # merges cleanly)
    checkout_keeping_workspace "$BRANCH" "origin/$BRANCH"
    echo "runner: continuing $BRANCH from origin/$BRANCH"
  elif git -C /workspace rev-parse --verify -q "refs/heads/$BRANCH" >/dev/null && \
     { ! git -C /workspace rev-parse --verify -q "origin/$DEFAULT_BRANCH" >/dev/null || \
       git -C /workspace merge-base --is-ancestor "origin/$DEFAULT_BRANCH" "refs/heads/$BRANCH"; }; then
    # The branch exists only LOCALLY (a previous run committed but never pushed -
    # crash, timeout, gate) and still contains the current base: continue it.
    # Re-anchoring here used to ORPHAN the finished commit and lose the work.
    git -C /workspace checkout -q "$BRANCH"
    echo "runner: continuing $BRANCH from local unpushed commits"
  elif git -C /workspace rev-parse --verify -q "origin/$DEFAULT_BRANCH" >/dev/null; then
    # no previous agent branch - or a STALE one that doesn't contain the current
    # base (customer pushed to $DEFAULT_BRANCH since, or it's a leftover from an
    # older build): basing on it would open a conflicting PR. Anchor on the base
    # branch instead; the workspace tree is the full deliverable and
    # force-with-lease rewrites the old branch.
    checkout_keeping_workspace "$BRANCH" "origin/$DEFAULT_BRANCH"
    echo "runner: anchored $BRANCH on origin/$DEFAULT_BRANCH"
  else
    # Before concluding the remote is empty, ask it which branch HEAD points at:
    # GIT_DEFAULT_BRANCH is the platform's guess and a main-vs-master mismatch
    # here used to turn a fully-populated repo into an orphan root-commit build.
    REMOTE_HEAD=$(git -C /workspace ls-remote --symref origin HEAD 2>/dev/null \
      | awk '/^ref:/ {sub("refs/heads/","",$2); print $2; exit}')
    if [[ -n "$REMOTE_HEAD" ]] && git -C /workspace rev-parse --verify -q "origin/$REMOTE_HEAD" >/dev/null; then
      checkout_keeping_workspace "$BRANCH" "origin/$REMOTE_HEAD"
      DEFAULT_BRANCH="$REMOTE_HEAD"
      echo "runner: anchored $BRANCH on origin/$REMOTE_HEAD (remote HEAD; GIT_DEFAULT_BRANCH mismatch)"
    else
      git -C /workspace checkout -q -B "$BRANCH"
      echo "runner: fresh branch $BRANCH (empty/new remote)"
    fi
  fi
elif git -C /workspace rev-parse --git-dir >/dev/null 2>&1; then
  git -C /workspace checkout -B "$BRANCH" || true
fi

# .openvisor/ (task, deploy key, KB fingerprints) must never be tracked. Append to
# the repo's .gitignore only when the entry is missing: an overwrite here gets
# clobbered by the checkout above when the repo tracks its own .gitignore - which
# is exactly how a deploy key once reached a customer PR - and would also drop the
# project's own ignore rules.
grep -qxF ".openvisor/" /workspace/.gitignore 2>/dev/null || echo ".openvisor/" >> /workspace/.gitignore

emit_event git "Repository ready"

# §working repositories: shallow-clone the customer's OTHER connected repos as
# read-only context (the task lists their paths). Under .openvisor/ so they are
# gitignored, never staged, and inside the leak-scan hard boundary. Best-effort:
# a failed context clone never fails the build.
if [[ -f "$OPENVISOR_DIR/context_repos.txt" ]]; then
  CTX_OK=0
  while read -r ctx_name ctx_uri; do
    [[ -z "$ctx_name" || -z "$ctx_uri" ]] && continue
    dest="$OPENVISOR_DIR/context/$ctx_name"
    rm -rf "$dest"
    if git clone -q --depth 1 "$ctx_uri" "$dest" 2>/dev/null; then
      CTX_OK=$((CTX_OK + 1))
      echo "runner: context repo $ctx_name cloned"
    else
      echo "runner: WARNING - context repo $ctx_name failed to clone (key not authorized?)"
      emit_event error "Context repository unavailable"
    fi
  done < "$OPENVISOR_DIR/context_repos.txt"
  [[ "$CTX_OK" -gt 0 ]] && emit_event git "Context repositories ready"
fi

if [[ "${SKIP_AGENT:-0}" == "1" ]]; then
  # Deterministic path: the worker pre-populated /workspace with a ready-to-ship
  # OCPA app; we skip the LLM run entirely (zero tokens) and just publish it.
  echo "runner: SKIP_AGENT=1 - publishing pre-populated workspace (no LLM run)" \
    | tee "$OPENVISOR_DIR/run.log"
  emit_event phase "Publishing the prepared workspace (no agent run)"
  STATUS=0
else
  # §dev harness: the platform names the driver this build runs on; this image
  # maps the id to a script. An id this image ships no driver for falls back to
  # the OpenHands driver rather than failing, so a newer platform dispatching to
  # an older runner still builds. The build panel's emit_event contract above
  # takes FIXED strings only, so the resolved name goes to stdout, never to it.
  DRIVER="/run_dev.py"
  case "${DEV_HARNESS:-openhands}" in
    openhands) DRIVER="/run_dev.py" ;;
    *) echo "runner: WARNING - unknown DEV_HARNESS '${DEV_HARNESS}' - using the OpenHands driver" ;;
  esac
  if [[ ! -f "$DRIVER" ]]; then
    echo "runner: WARNING - driver $DRIVER is not in this image - using /run_dev.py"
    DRIVER="/run_dev.py"
  fi
  echo "runner: starting headless build (harness=${DEV_HARNESS:-openhands}, driver=$DRIVER, model=$LLM_MODEL)"
  emit_event phase "Agent session opened"
  set +e
  # -u: unbuffered driver output so .openvisor/run.log grows live, not at exit.
  python -u "$DRIVER" > "$OPENVISOR_DIR/run.log" 2>&1
  STATUS=$?
  set -e
  echo "runner: driver $DRIVER exited $STATUS"
  emit_event phase "Agent session closed"
  [[ -f "$OPENVISOR_DIR/run.log" ]] && tail -n 20 "$OPENVISOR_DIR/run.log" || true
fi

# §working method plan gate: a PLAN_ONLY pass explores and writes
# .openvisor/plan.md but never publishes - the worker reads the plan off the
# workspace volume and asks the customer to approve it. Discard any working-tree
# edits so a plan run can never smuggle changes into the next publish.
if [[ "${PLAN_ONLY:-0}" == "1" ]]; then
  if [[ -s "$OPENVISOR_DIR/plan.md" ]]; then
    emit_event phase "Plan written - awaiting your approval"
  else
    echo "runner: WARNING - plan run produced no plan.md"
  fi
  git -C /workspace checkout -q -- . 2>/dev/null || true
  git -C /workspace clean -qfd 2>/dev/null || true
  exit "$STATUS"
fi

# Publish the agent's branch. Only the platform GitLab (GIT_PROVIDER=gitlab) uses
# the push options that open the MR directly; GitHub, a customer's own GitLab
# (gitlab_customer) and unrecognised hosts (other) push plain, and the worker
# opens the PR/MR via the REST API (or the customer merges). Skipped when
# GIT_PUSH!=1.
PROVIDER="${GIT_PROVIDER:-gitlab}"
if [[ "${GIT_PUSH:-0}" == "1" ]] && git -C /workspace remote get-url origin >/dev/null 2>&1; then
  # Heal workspaces where a pre-PYTHONDONTWRITEBYTECODE run shed the driver's own
  # .pyc caches (runner-internal, never part of the deliverable). Narrow to our
  # module names - a project's own build artifacts are its own business.
  rm -f /workspace/__pycache__/live_events.*.pyc /workspace/__pycache__/run_dev.*.pyc \
    /workspace/__pycache__/leak_scan.*.pyc 2>/dev/null || true
  rmdir /workspace/__pycache__ 2>/dev/null || true
  git -C /workspace add -A
  # Whatever state .gitignore ended up in, .openvisor/ leaves the index here; this
  # also stages the DELETION of .openvisor files a past run leaked into the repo.
  # leak_scan.py still hard-blocks on any .openvisor path that survives.
  git -C /workspace rm -r -q --cached --ignore-unmatch .openvisor
  # §publish gate: refuse to publish a run that produced NOTHING - a capped or
  # errored agent session must not ship a plumbing-only commit (the .openvisor/
  # .gitignore append) as a fresh PR. Meaningful = any staged change beyond that
  # line, a real .gitignore edit by the agent, or committed branch content vs the
  # base (a continued run keeps its earlier work). Skipped for the deterministic
  # scaffold and when the base branch is unknown (empty/new repo).
  if [[ "${SKIP_AGENT:-0}" != "1" ]] && git -C /workspace rev-parse --verify -q "origin/$DEFAULT_BRANCH" >/dev/null; then
    STAGED_REAL=$(git -C /workspace diff --cached --name-only -- . ':(exclude).gitignore' | head -1 || true)
    # grep exits 1 on no match - harmless here, but fatal under pipefail+set -e.
    GITIGNORE_REAL=$(git -C /workspace diff --cached -- .gitignore | { grep -E '^[+-]' || true; } | { grep -vE '^(\+\+\+|---)' || true; } | { grep -vxF '+.openvisor/' || true; } | head -1)
    BRANCH_REAL=$(git -C /workspace diff --name-only "origin/$DEFAULT_BRANCH...HEAD" -- . ':(exclude).gitignore' 2>/dev/null | head -1 || true)
    # A COMMITTED .gitignore change (beyond the .openvisor/ plumbing line) is a
    # real deliverable too - excluding .gitignore wholesale from the branch diff
    # made a branch whose intended change WAS an ignore rule read as empty, and
    # the customer was told "nothing was committed" over a commit sitting right
    # on the branch. The pure-plumbing commit this gate exists to block still
    # is: its only line is '+.openvisor/'.
    BRANCH_GITIGNORE_REAL=$(git -C /workspace diff "origin/$DEFAULT_BRANCH...HEAD" -- .gitignore 2>/dev/null | { grep -E '^[+-]' || true; } | { grep -vE '^(\+\+\+|---)' || true; } | { grep -vxF '+.openvisor/' || true; } | head -1)
    if [[ -z "$STAGED_REAL" && -z "$GITIGNORE_REAL" && -z "$BRANCH_REAL" && -z "$BRANCH_GITIGNORE_REAL" ]]; then
      echo "runner: NO_CHANGES_TO_PUBLISH - the agent session produced no changes; not publishing" >&2
      # §investigation runs: an investigation that honestly concluded "nothing to
      # change" lands on this same gate, and the worker finishes it as the
      # delivered answer it is (_finish_investigation). Only the UNEXPECTED empty
      # session is an error - a declared no_change_needed reading as a red error
      # line was the last thing the customer saw on a run that went perfectly.
      # The exit code is unchanged: it is the worker's contract either way.
      if [[ -f "$OPENVISOR_DIR/outcome.json" ]] && grep -Eq \
          '"outcome"[[:space:]]*:[[:space:]]*"no_change_needed"' \
          "$OPENVISOR_DIR/outcome.json" 2>/dev/null; then
        emit_event finish "No repository change was needed"
      else
        emit_event error "No changes produced - nothing to publish"
      fi
      exit 5
    fi
  fi
  # Hard exfiltration boundary (defence-in-depth behind development_system.md rules
  # 9-11): never commit/push files that contain platform secrets or verbatim
  # knowledge-base text. A hit fails the run instead of publishing to the customer.
  emit_event scan "Running the confidentiality leak scan"
  if ! python /leak_scan.py; then
    echo "runner: LEAK_SCAN_BLOCKED - refusing to commit or push this build" >&2
    emit_event error "Publication blocked by the leak scan"
    STATUS=3
  else
    # The agent often commits (and sometimes pushes) its own work: the wrapper
    # commit is best-effort, and the PUSH must happen regardless - the §publish
    # gate above already guaranteed there is branch content to publish, and on
    # the platform path this push carries the MR-creating push options.
    if ! git -C /workspace commit -m "${BRAND_NAME:-Openvisor} agent: MVP build"; then
      echo "runner: nothing new to commit - publishing the branch as the agent committed it"
    fi
    # Commits ship under the configured git identity ALONE: the agent's commit
    # tooling appends its own attribution trailer (Co-authored-by: openhands
    # <...>) that no task instruction can suppress, so it is scrubbed
    # deterministically here - the commit-message twin of the worker's
    # _strip_commit_trailers on PR/MR descriptions. Only the UNPUSHED range is
    # rewritten: never commits already on the remote branch, never history
    # below the branch's own work.
    SCRUB_BASE=""
    if git -C /workspace rev-parse --verify -q "origin/$BRANCH" >/dev/null; then
      SCRUB_BASE="origin/$BRANCH"
    elif git -C /workspace rev-parse --verify -q "origin/$DEFAULT_BRANCH" >/dev/null; then
      SCRUB_BASE=$(git -C /workspace merge-base "origin/$DEFAULT_BRANCH" HEAD 2>/dev/null || true)
    fi
    if [[ -n "$SCRUB_BASE" ]] && git -C /workspace log --format=%B "$SCRUB_BASE..HEAD" 2>/dev/null \
        | grep -qiE '^[[:space:]]*(Co-authored-by|Signed-off-by):'; then
      echo "runner: scrubbing tool-attribution trailers from unpushed commit messages"
      emit_event git "Removing tool-attribution trailers from commit messages"
      if ! FILTER_BRANCH_SQUELCH_WARNING=1 git -C /workspace filter-branch -f \
          --msg-filter "sed -E '/^[[:space:]]*(Co-authored-by|Signed-off-by):/Id'" \
          -- "$SCRUB_BASE..HEAD" >/dev/null 2>&1; then
        echo "runner: WARNING - trailer scrub failed; publishing the branch as committed"
      fi
    fi
    if [[ "$PROVIDER" == "gitlab" ]]; then
      # No merge_request.title option: GitLab applies merge_request.* push
      # options to the EXISTING MR on every later push, so a fixed title here
      # would retitle an agent-authored MR on each recovery dispatch. create+
      # target are idempotent; the worker personalizes the title/description
      # right after (_personalize_platform_mr).
      PUSH_ARGS=(push -u origin "$BRANCH" --force-with-lease \
        -o merge_request.create -o merge_request.target="$DEFAULT_BRANCH")
    else
      PUSH_ARGS=(push -u origin "$BRANCH" --force-with-lease)
    fi
    PUSHED=1
    if ! git -C /workspace "${PUSH_ARGS[@]}"; then
      # Most rejections are a stale --force-with-lease (the remote moved - or
      # dropped the branch on merge - after our fetch): refresh the lease and
      # retry once. agent/mvp is platform-owned, so overwriting is the intent.
      echo "runner: push rejected - refreshing remote refs and retrying"
      emit_event git "Push rejected - refreshing the remote state to retry"
      git -C /workspace fetch -q --prune origin 2>/dev/null || true
      if ! git -C /workspace "${PUSH_ARGS[@]}"; then
        PUSHED=0
      fi
    fi
    if [[ "$PUSHED" == 1 ]]; then
      echo "runner: pushed $BRANCH to $PROVIDER"
      emit_event git "Branch pushed for review"
    else
      # PUSH_FAILED is the worker's sentinel (like LEAK_SCAN_BLOCKED): the build
      # must park as failed+resumable, not publish/merge as if it had shipped.
      # Keep a driver error as the exit code - it is the deeper failure.
      echo "runner: PUSH_FAILED - the branch was not published" >&2
      emit_event error "Pushing the branch failed - parking the build for a retry"
      if [[ "$STATUS" == 0 ]]; then
        STATUS=4
      fi
    fi
  fi
fi

exit $STATUS
