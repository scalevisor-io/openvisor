# End-to-end check in CI (CI e2e)

`.github/workflows/e2e.yml` replays the customer <-> consultant walkthrough of [`.claude/commands/e2e-check.md`](../../.claude/commands/e2e-check.md) headless, against the real compose stack, on every push to `main` and every pull request. It is the regression test for the whole product path: signup -> email verification -> project deposit with an existing repository -> onboarding answers -> evaluation -> submit -> Memory (plain + secret) -> a feature Request -> a chat message -> the free human-answer escalation -> admin pricing (`payment_due`, customer emailed) -> credit grant -> auto-advance to `development` -> the §14 pipeline (push preflight, scaffold, runner publish, boot gate) pushes the build branch -> the customer merges it -> the merge sweep deploys the demo -> the demo answers through Traefik with basic auth -> delivery approval -> `finished`.

## What runs

| Piece | Role |
| --- | --- |
| [`e2e.py`](e2e.py) | The walkthrough as HTTP calls (two `requests` sessions: customer and admin). Prints one `PASS`/`FAIL` line per step with its evidence and exits non-zero on the first failure. |
| [`env.ci`](env.ci) | The job's `.env`. Throwaway values only; the model endpoints point at `127.0.0.1:1` so every LLM call fails instantly and the `DEPLOY_ENV=local` heuristics take over. `OPENHANDS_ENABLED=0` drives the deterministic scaffold through the exact same push -> merge -> deploy path as an agent run, at zero token cost. |
| [`../compose.e2e.yml`](../compose.e2e.yml) + [`gitserver/`](gitserver/) | The customer's git hosting: sshd + git serving one bare repository (`ssh://git@gitserver/srv/git/todo.git`, `main` seeded with one commit) on the platform `internal` network, so the worker and the runner sandbox both resolve it by name. No forge, no token. |
| [`hooks/install-key.sh`](hooks/install-key.sh) | `E2E_INSTALL_KEY_CMD`: installs the project's deploy key (stdin) on the git server, the step a customer does on GitHub with `gh repo deploy-key add --allow-write`. |
| [`hooks/merge.sh`](hooks/merge.sh) | `E2E_MERGE_CMD`: merges the build branch (`$E2E_BRANCH`) into `main` with a real merge commit, the step a customer does with `gh pr merge`. The platform's merge sweep (`dev_pr_sweep`, every 60 s) detects it over SSH and deploys the demo. |

The two hooks are the only repository-specific steps, so the same script replays against a real GitHub repository by swapping them for `gh` commands.

The job starts only the services on that path (`postgres redis meilisearch api worker beat deployer traefik mailpit gitserver`): the SPA, landing, MCP sidecars and the web-research image are not exercised and would not fit a hosted runner's disk. The runner image is built with the GitHub Actions layer cache because the scaffold path still launches the runner sandbox for the git publish. Demos and the boot gate run as `--privileged` Docker-in-Docker (the local fallback when `DEMO_RUNTIME` is empty), which hosted runners allow.

## Replaying it locally

Against a worktree slot (see CLAUDE.md, "Running several instances side by side"), with the slot's `.env` switched to the CI toggles (`OPENHANDS_ENABLED=0`, `ALTCHA_ENABLED=0`, the model trio at `http://127.0.0.1:1/v1`, `LLM_MAX_RETRIES=0`; restart `api worker beat` afterwards):

```bash
export COMPOSE="docker compose -f compose.base.yml -f compose.dev.yml -f ci/compose.e2e.yml"
$COMPOSE up -d --build gitserver
E2E_BASE=http://127.0.0.1:8090 E2E_APP_HOST=app.openvisor2.local E2E_MAIL_HOST=mail.openvisor2.local \
ADMIN_EMAIL=... ADMIN_PASSWORD=... \
E2E_REPO_SSH_URI=ssh://git@gitserver/srv/git/todo.git \
E2E_INSTALL_KEY_CMD="sh ci/e2e/hooks/install-key.sh" E2E_MERGE_CMD="sh ci/e2e/hooks/merge.sh" \
python3 ci/e2e/e2e.py
```

Every run creates a fresh customer (`e2e+<timestamp>@example.com`) and a fresh project; the git server keeps the merged history, so a later run simply branches from the previous demo's `main`. `$COMPOSE rm -sf gitserver` resets it.

## Environment contract of `e2e.py`

| Variable | Meaning |
| --- | --- |
| `E2E_BASE` | Traefik entrypoint, e.g. `http://127.0.0.1:8080`. |
| `E2E_APP_HOST`, `E2E_MAIL_HOST` | `Host` headers for the app/API and for Mailpit. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | The seeded admin (consultant) account. |
| `E2E_REPO_SSH_URI` | The customer repository the project connects to as its push target. |
| `E2E_INSTALL_KEY_CMD` | Shell command that receives the project's public deploy key on stdin and installs it with write access. |
| `E2E_MERGE_CMD` | Shell command that merges `$E2E_BRANCH` into the repository's `main`. |
| `E2E_ALTCHA` | `1` to solve the proof-of-work captcha on signup and login (default `0`: the gate is off in CI). |
| `E2E_BUILD_TIMEOUT`, `E2E_DEMO_TIMEOUT` | Seconds to wait for the build to reach `awaiting_merge` (default 900) and for the demo after the merge (default 300). |
