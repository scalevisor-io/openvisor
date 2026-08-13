import { useEffect, useState } from "react";
import { memoryApi, projectsApi } from "../lib/endpoints";
import { useToast } from "../lib/toast";
import type { Project, RepoProvider } from "../types";
import { CollapsibleCard, CopyField, Spinner, Toggle, readCardCollapsed } from "./ui";

/** Client-side host guess: only to decide whether to ask the user to pick a
 * platform when the host is unrecognisable. The server re-detects on connect. */
function guessProvider(url: string): RepoProvider | null {
  if (/github\.com/i.test(url)) return "github";
  if (/gitlab/i.test(url)) return "gitlab";
  return null;
}

const PROVIDER_LABEL: Record<RepoProvider, string> = {
  github: "GitHub",
  gitlab: "GitLab",
  other: "Other host",
};

/** One dense working-repo option: short label + ⓘ + toggle, the explanation in the tooltip. */
function OptionToggle({
  label,
  hint,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <span className="row gap-sm" style={{ alignItems: "center" }} title={hint}>
      <span className="tiny">
        {label}{" "}
        <span style={{ cursor: "help", opacity: 0.7 }} aria-label={hint}>
          ⓘ
        </span>
      </span>
      <Toggle checked={checked} disabled={disabled} onChange={onChange} title={hint} />
    </span>
  );
}

export default function ReposCard({
  project,
  onProjectChange,
  onGoToMemory,
  consultant,
}: {
  project: Project;
  onProjectChange: (p: Project) => void;
  onGoToMemory: () => void;
  consultant: string;
}) {
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [providerOverride, setProviderOverride] = useState<RepoProvider>("other");
  const [connectBusy, setConnectBusy] = useState(false);
  const [busyRepo, setBusyRepo] = useState<string | null>(null);
  // Per-repo "Test connection" result (auth check), keyed by repo id.
  const [authResult, setAuthResult] = useState<Record<string, { ok: boolean; detail: string }>>({});
  // Per-repo "Check SSH" result (deploy-key reachability) + which repo is checking.
  const [sshResult, setSshResult] = useState<Record<string, { ok: boolean; detail: string }>>({});
  const [sshBusy, setSshBusy] = useState<string | null>(null);

  const id = project.id;
  const repos = project.repos;
  const platform = project.platform_repo;
  const typed = url.trim();
  const ambiguous = typed.length > 0 && guessProvider(typed) === null;
  const pushRepo = repos.find((r) => r.is_push_target) || null;

  // Section collapse + the "connection verified" header check (a successful
  // Test connection collapses the card). User toggles persist across reloads;
  // the programmatic collapse-on-verify stays session-only.
  const [collapsed, setCollapsed] = useState(() => readCardCollapsed("project:repos") ?? false);
  const [verified, setVerified] = useState(false);

  // Provider API token (GITHUB_TOKEN / GITLAB_TOKEN Memory secret): required to
  // open the PR/MR and, for auto_dev, to watch the repo's issues. The input here
  // upserts the Memory entry directly so the user never leaves the section.
  const tokenKey = pushRepo?.provider === "github" ? "GITHUB_TOKEN"
    : pushRepo?.provider === "gitlab" ? "GITLAB_TOKEN" : null;
  const [tokenSet, setTokenSet] = useState<boolean | null>(null);
  const [tokenInput, setTokenInput] = useState("");
  const [tokenBusy, setTokenBusy] = useState(false);

  useEffect(() => {
    if (!tokenKey) return;
    let cancelled = false;
    memoryApi
      .list(id)
      .then((entries) => !cancelled && setTokenSet(entries.some((e) => e.key === tokenKey)))
      .catch(() => !cancelled && setTokenSet(null));
    return () => {
      cancelled = true;
    };
  }, [id, tokenKey]);

  async function saveToken() {
    if (!tokenKey || !tokenInput.trim()) return;
    setTokenBusy(true);
    try {
      await memoryApi.upsert(id, {
        key: tokenKey,
        value: tokenInput.trim(),
        is_secret: true,
        description: "API token used to open the pull/merge request (and watch issues)",
      });
      setTokenSet(true);
      setTokenInput("");
      toast.push(`${tokenKey} saved to Memory.`, "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not save the token", "err");
    } finally {
      setTokenBusy(false);
    }
  }

  // Where to create the token, on the customer's own instance.
  const tokenCreateUrl = pushRepo?.provider === "github"
    ? "https://github.com/settings/tokens/new?scopes=repo"
    : pushRepo?.provider === "gitlab"
      ? `https://${(pushRepo.ssh_uri.split("@")[1] || "gitlab.com").split(/[:/]/)[0]}/-/user_settings/personal_access_tokens?scopes=api`
      : null;

  async function run(repoKey: string, fn: () => Promise<Project>, ok?: string) {
    setBusyRepo(repoKey);
    try {
      onProjectChange(await fn());
      if (ok) toast.push(ok, "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Something went wrong", "err");
    } finally {
      setBusyRepo(null);
    }
  }

  async function connect() {
    if (!typed) return;
    setConnectBusy(true);
    try {
      const updated = await projectsApi.connectRepo(id, typed, ambiguous ? providerOverride : undefined);
      onProjectChange(updated);
      setUrl("");
      toast.push("Repository connected.", "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to connect the repository", "err");
    } finally {
      setConnectBusy(false);
    }
  }

  async function testAuth(repoId: string) {
    setBusyRepo(repoId);
    try {
      const res = await projectsApi.verifyRepoAuth(id, repoId);
      setAuthResult((r) => ({ ...r, [repoId]: res }));
      if (res.ok) {
        // Verified: badge the section header and fold the card out of the way.
        setVerified(true);
        setCollapsed(true);
        toast.push("Connection verified - repository section collapsed.", "ok");
      }
    } catch (err) {
      setAuthResult((r) => ({
        ...r,
        [repoId]: { ok: false, detail: err instanceof Error ? err.message : "Check failed" },
      }));
    } finally {
      setBusyRepo(null);
    }
  }

  // SSH reachability over the project deploy key: read-only, so it doesn't lock the
  // other repo controls (its own sshBusy key drives the spinner). Toasts on failure.
  async function checkSsh(repoId: string) {
    setSshBusy(repoId);
    try {
      const res = await projectsApi.verifySsh(id, repoId);
      setSshResult((r) => ({ ...r, [repoId]: res }));
      if (!res.ok) toast.push(res.detail, "err");
    } catch (err) {
      const detail = err instanceof Error ? err.message : "Check failed";
      setSshResult((r) => ({ ...r, [repoId]: { ok: false, detail } }));
      toast.push(detail, "err");
    } finally {
      setSshBusy(null);
    }
  }

  return (
    <CollapsibleCard
      title="Repositories"
      check={verified}
      collapsed={collapsed}
      onToggle={setCollapsed}
      storageKey="project:repos"
      subtitle="tokens · deploy key · working repos"
    >
      <p className="tiny faint">
        Connect the repositories {consultant}'s agent should work with. EVERY connected repo is
        cloned into the agent's workspace as read-only context; the selected one is the working
        repo - the agent commits there, pushes its branch and opens the PR/MR. Add the deploy key
        below to each repo you connect, then use Check SSH to confirm it's reachable before
        development.
      </p>

      {tokenKey && tokenCreateUrl && (
        <div className="field mt">
          <label>
            {tokenKey}{" "}
            <span
              title={`The deploy key only lets the agent PUSH its branch. This ${PROVIDER_LABEL[pushRepo!.provider]} token lets it OPEN the ${pushRepo!.provider === "github" ? "pull request" : "merge request"}, run auto-merge${project.kind === "auto_dev" ? ", and watch this repository's issues" : ""}. Stored as a secret in the project Memory.`}
              style={{ cursor: "help", opacity: 0.7 }}
              aria-label="What is this token for?"
            >
              ⓘ
            </span>{" "}
            <a href={tokenCreateUrl} target="_blank" rel="noreferrer" className="tiny">
              Create one on {PROVIDER_LABEL[pushRepo!.provider]} ↗
            </a>
          </label>
          <div className="row gap-sm">
            <input
              type="password"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              placeholder={pushRepo!.provider === "github" ? "ghp_… / github_pat_…" : "glpat-…"}
              autoComplete="new-password"
              style={{ flex: 1 }}
            />
            <button
              className="btn btn-sm"
              disabled={tokenBusy || !tokenInput.trim()}
              onClick={saveToken}
            >
              {tokenBusy ? <Spinner /> : "Save"}
            </button>
          </div>
          <p className={`tiny mt ${tokenSet ? "ok-text" : "faint"}`}>
            {tokenSet
              ? "✓ Token set - saving a new value replaces it."
              : tokenSet === null
                ? "Needed to open the "
                  + (pushRepo!.provider === "github" ? "pull request" : "merge request")
                  + (project.kind === "auto_dev" ? " and watch issues" : "")
                  + "."
                : "No token yet - without it the agent only pushes its branch and you open the "
                  + (pushRepo!.provider === "github" ? "PR" : "MR")
                  + " yourself."}
          </p>
        </div>
      )}

      <div className="field mt">
        <label>Add SSH key in account or as project deploy key (for development)</label>
        <CopyField value={project.ssh_public_key || "-"} block />
      </div>

      {/* Push-target list: platform repo + connected repos, exactly one selected.
          The connect input sits at the top so adding a repo never means scrolling
          past the list and the options. */}
      <div className="mt">
        <label>Working repositories</label>

        <div className="row gap-sm mt">
          <input
            type="text"
            style={{ flex: 1 }}
            placeholder="git@github.com:you/repo.git"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          {ambiguous && (
            <select
              value={providerOverride}
              onChange={(e) => setProviderOverride(e.target.value as RepoProvider)}
              title="Unrecognised host - pick the platform"
            >
              <option value="other">{PROVIDER_LABEL.other}</option>
              <option value="github">{PROVIDER_LABEL.github}</option>
              <option value="gitlab">{PROVIDER_LABEL.gitlab}</option>
            </select>
          )}
          <button className="btn btn-sm" disabled={connectBusy || !typed} onClick={connect}>
            {connectBusy ? <Spinner /> : "Connect"}
          </button>
        </div>
        {ambiguous && (
          <p className="tiny faint mt">
            We couldn't tell if this is GitHub or GitLab from the URL - pick the platform so
            auto-merge can work (or leave it as Other host to just push a branch).
          </p>
        )}

        {platform && (
          <div className="row gap-sm between mt">
            <label className="row gap-sm" style={{ margin: 0, cursor: "pointer", flex: 1 }}>
              <input
                type="radio"
                name="push-target"
                checked={platform.is_push_target}
                disabled={busyRepo !== null}
                onChange={() => run("platform", () => projectsApi.usePlatformRepo(id))}
              />
              <span>
                <strong>Platform repo</strong> <span className="tiny faint">gitlab · auto-generated</span>
                <div className="tiny mono muted">
                  {platform.web_url ? (
                    <a href={platform.web_url} target="_blank" rel="noreferrer">
                      {platform.web_url}
                    </a>
                  ) : platform.provisioned ? (
                    platform.ssh_url
                  ) : (
                    <span className="faint">Provisioned after review.</span>
                  )}
                </div>
              </span>
            </label>
          </div>
        )}

        {repos.map((r) => (
          <div key={r.id} className="mt">
            <div className="row gap-sm between">
              <label className="row gap-sm" style={{ margin: 0, cursor: "pointer", flex: 1 }}>
                <input
                  type="radio"
                  name="push-target"
                  checked={r.is_push_target}
                  disabled={busyRepo !== null}
                  onChange={() => run(r.id, () => projectsApi.updateRepo(id, r.id, { is_push_target: true }))}
                />
                <span style={{ flex: 1 }}>
                  <span className="mono tiny">{r.ssh_uri}</span>{" "}
                  <span className="tiny faint">{r.provider}</span>
                </span>
              </label>
              <button
                className="btn btn-sm btn-ghost"
                disabled={sshBusy !== null}
                onClick={() => checkSsh(r.id)}
                title="Verify the host is reachable and this project's SSH deploy key has access"
              >
                {sshBusy === r.id ? <Spinner /> : "Check SSH"}
              </button>
              <button
                className="btn btn-sm btn-ghost"
                disabled={busyRepo !== null}
                onClick={() => run(r.id, () => projectsApi.removeRepo(id, r.id))}
                title="Disconnect"
              >
                Remove
              </button>
            </div>
            {sshResult[r.id] && (
              <p className={`tiny mt ${sshResult[r.id].ok ? "ok-text" : "danger"}`}>
                {sshResult[r.id].ok ? "✓ " : "✗ "}
                {sshResult[r.id].detail}
              </p>
            )}
          </div>
        ))}

        {repos.length === 0 && !platform && (
          <p className="tiny faint mt">No repository yet - connect one above.</p>
        )}
      </div>

      {pushRepo && pushRepo.provider === "other" && (
        <p className="tiny faint mt">
          This host has no PR/MR API integration: the agent pushes its <code>agent/mvp</code>{" "}
          branch over the deploy key and you merge it yourself.
        </p>
      )}

      {/* Working-repo options for the connected push repo (github/gitlab only):
          one dense wrapping row of label+ⓘ+toggle chips - the detail lives in
          each chip's tooltip. */}
      {pushRepo && (
        <div className="mt">
          {pushRepo.can_auto_merge ? (
            <>
              <label style={{ margin: 0 }}>Working repo options</label>
              <div className="row mt" style={{ flexWrap: "wrap", columnGap: 24, rowGap: 8 }}>
                <OptionToggle
                  label="Auto-merge"
                  hint={`${consultant}'s agent security-reviews its ${pushRepo.provider === "github" ? "pull request" : "merge request"} and merges it automatically, fixing any issues before asking you to review. Requires a ${pushRepo.provider === "github" ? "GITHUB_TOKEN" : "GITLAB_TOKEN"} secret in Memory whose access to this repo checks out.`}
                  checked={pushRepo.auto_merge}
                  disabled={busyRepo !== null}
                  onChange={(v) =>
                    run(pushRepo.id, () => projectsApi.updateRepo(id, pushRepo.id, { auto_merge: v }),
                      v ? "Auto-merge enabled." : "Auto-merge disabled.")
                  }
                />
                <OptionToggle
                  label="Squash on merge"
                  hint="When the agent merges automatically, its work lands as a single squashed commit instead of the branch's full commit history."
                  checked={pushRepo.squash_on_merge}
                  disabled={busyRepo !== null}
                  onChange={(v) =>
                    run(pushRepo.id, () => projectsApi.updateRepo(id, pushRepo.id, { squash_on_merge: v }),
                      v ? "Squash on merge enabled." : "Squash on merge disabled.")
                  }
                />
                {project.kind === "auto_dev" && (
                  <OptionToggle
                    label="Summarize to issue"
                    hint="When a build was triggered by one of this repository's issues, the agent appends a summary of the work it did to the comment it posts back on that issue alongside the PR/MR link."
                    checked={pushRepo.summarize_to_issue}
                    disabled={busyRepo !== null}
                    onChange={(v) =>
                      run(pushRepo.id, () => projectsApi.updateRepo(id, pushRepo.id, { summarize_to_issue: v }),
                        v ? "Issue summaries enabled." : "Issue summaries disabled.")
                    }
                  />
                )}
              </div>
              <div className="row gap-sm mt">
                <button
                  className="btn btn-sm"
                  disabled={busyRepo !== null}
                  onClick={() => testAuth(pushRepo.id)}
                >
                  {busyRepo === pushRepo.id ? <Spinner /> : "Test connection"}
                </button>
                <button className="btn btn-sm btn-ghost" onClick={onGoToMemory}>
                  Manage token in Memory
                </button>
              </div>
              {authResult[pushRepo.id] && (
                <p className={`tiny mt ${authResult[pushRepo.id].ok ? "ok-text" : "danger"}`}>
                  {authResult[pushRepo.id].ok ? "✓ " : "✗ "}
                  {authResult[pushRepo.id].detail}
                </p>
              )}
            </>
          ) : (
            <p className="tiny faint mt">
              Auto-merge is only available for GitHub or GitLab repositories. This host isn't
              recognised, so the agent just pushes its branch for you to merge.
            </p>
          )}
        </div>
      )}

    </CollapsibleCard>
  );
}
