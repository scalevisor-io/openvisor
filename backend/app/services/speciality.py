"""Per-speciality profile (§Phase 2): the specialities.json fields that no code read
(deliverable_type, knowledge_tags) become behaviour. A speciality is a PROFILE on the
ONE harness, not a second architecture.

The first profile lever is the deliverable-type prompt overlay: a report track
(audit_report / architecture_docs) is told to produce its deliverable as a browsable
static site served via the normal compose.demo.yml contract - so the entire existing
pipeline (contract lint, boot gate, demo deploy, approval, even the sovereign gate)
delivers a REPORT unchanged, instead of the boot gate parking it for lacking a demo.
"""
from __future__ import annotations

from app.services.pricing import load_static

_REPORT_TYPES = ("audit_report", "architecture_docs")


def profile(speciality_id: str | None) -> dict:
    """The speciality's record from specialities.json (or {} if unknown/unset)."""
    if not speciality_id:
        return {}
    try:
        for s in load_static("specialities.json")["specialities"]:
            if s.get("id") == speciality_id:
                return s
    except Exception:  # noqa: BLE001 - a missing/broken spec file must never fail a build
        return {}
    return {}


def deliverable_type(project) -> str:
    # §auto_dev: the sentinel's outcome is always a pull request on the
    # customer's existing repo - never a deployed demo, whatever the speciality.
    if getattr(project, "kind", None) == "auto_dev":
        return "pull_request"
    return profile(getattr(project, "speciality", None)).get("deliverable_type", "deployed_demo")


def is_report_track(project) -> bool:
    """True when the deliverable is a document (report/docs), not a running app."""
    return deliverable_type(project) in _REPORT_TYPES


# ---- §fees: the instance-effective engagement fee ----

# AppSetting key holding the admin's per-speciality fee overrides:
# {speciality_id: credits}. Absent id = the specialities.json default applies.
FEE_OVERRIDES_KEY = "speciality_fee_overrides"


def clean_fee(value) -> float | None:
    """A usable fee or None: missing, garbled, negative or non-finite values
    read as unset (the hub coerces identically on its side)."""
    try:
        fee = float(value)
    except (TypeError, ValueError):
        return None
    if fee != fee or fee in (float("inf"), float("-inf")) or fee < 0:
        return None
    return round(fee, 2)


def effective_base_fee(spec: dict, overrides: dict | None) -> float:
    """The fee a track actually charges on THIS instance: the admin override
    (AppSetting FEE_OVERRIDES_KEY, set on /admin/settings) wins over the
    specialities.json default. Every reader - the evaluation estimate, the
    public /api/settings, the hub enrollment report - resolves here, so the
    charged fee and the advertised fee cannot drift."""
    if overrides and isinstance(overrides, dict):
        override = clean_fee(overrides.get(spec.get("id")))
        if override is not None:
            return override
    return clean_fee(spec.get("base_fee_credits")) or 0.0


def is_devsecops(project) -> bool:
    """True when the DevSecOps hardening OVERLAY applies (SBOM + CVE gate). Derived
    from the devsecops-hardened speciality today; this predicate is the single seam
    so promoting DevSecOps to a composable cross-cutting overlay flag later (like
    project.sovereign) is a one-line change here, not a pipeline rewrite."""
    return getattr(project, "speciality", None) == "devsecops-hardened"


def knowledge_tags(project) -> list:
    return profile(getattr(project, "speciality", None)).get("knowledge_tags", []) or []


_AUDIT_REPORT = (
    "\n**YOUR DELIVERABLE IS A REPORT, NOT A GENERAL APPLICATION.** This is a supply-chain "
    "audit. Analyse the target described in the project context (its dependencies, "
    "containers, and manifests) and PRODUCE A REPORT: a Software Bill of Materials (SBOM), "
    "a dependency inventory, known-CVE findings with severities, license risks, and "
    "prioritised, actionable remediations. The report CONTENT is the deliverable. Deliver "
    "it as a BROWSABLE STATIC SITE: render the report to clean, well-structured HTML (a "
    "summary, tables of findings, clear sections) and serve it with a tiny static web "
    "server, shipping the SAME compose.base.yml + compose.demo.yml contract (one "
    "static-server service published on $PORT) so the customer reads the report in their "
    "browser. Do NOT build a general web app - the 'app' is just the static server that "
    "serves your report.\n"
)
_ARCH_DOCS = (
    "\n**YOUR DELIVERABLE IS AN ARCHITECTURE REVIEW, NOT A GENERAL APPLICATION.** Analyse "
    "the system described in the project context and PRODUCE ARCHITECTURE DOCUMENTATION: a "
    "component / C4-style breakdown, the data flows, identified risks and bottlenecks, and "
    "prioritised recommendations. The documentation CONTENT is the deliverable. Deliver it "
    "as a BROWSABLE STATIC SITE: render it to clean HTML (diagrams as inline SVG/images, "
    "clear sections) and serve it with a tiny static web server, shipping the SAME "
    "compose.base.yml + compose.demo.yml contract (one static-server service on $PORT). "
    "Do NOT build a general web app - the 'app' is just the static server for your docs.\n"
)


_PULL_REQUEST = """
DELIVERABLE OVERRIDE - focused pull request, NOT a deployed demo. You are
implementing a scoped change request on the customer's EXISTING repository:
- The deliverable is the requested change itself, as a minimal reviewable diff
  that follows the repository's own structure, conventions and tooling.
- There is NO demo boot contract for this run: do NOT create compose.base.yml,
  compose.demo.yml, Dockerfiles, server stubs or any scaffolding the request
  didn't ask for. Never add files whose only purpose is to make the repository
  "bootable" for this platform.
- Verify with the repository's OWN checks (its tests, linters, CI config) - your
  plan's success check must come from the repo, not from an HTTP demo probe.
"""


def deliverable_clause(project) -> str:
    """The prompt overlay for this project's deliverable type. Empty for a normal
    deployed_demo (the default MVP-app instructions stand)."""
    dt = deliverable_type(project)
    if dt == "pull_request":
        return _PULL_REQUEST
    if dt == "audit_report":
        return _AUDIT_REPORT
    if dt == "architecture_docs":
        return _ARCH_DOCS
    return ""


# §Phase 2 one-shot demonstration: a concrete worked example of the deliverable's boot
# contract. The best-evidenced (and cheapest) specialization lever - it targets the #1
# harness failure class (contract/boot violations: a Dockerfile that forgets a file, a
# server bound to a different port than compose publishes).
_SCAFFOLD_DEMO = """

## Worked example - the demo boot contract (adapt to your stack; keep this shape)
A deliverable that reliably boots looks like this. The server listens on the SAME
internal port that `compose.demo.yml` publishes as `${PORT}`, and the Dockerfile COPYs
everything the entrypoint needs (prefer `COPY . .` + a `.dockerignore`).

`compose.base.yml`
```yaml
services:
  web:
    build: .
    restart: unless-stopped
```
`compose.demo.yml`
```yaml
services:
  web:
    ports:
      - "${PORT}:8080"
```
`Dockerfile`
```dockerfile
FROM node:20-slim
WORKDIR /app
COPY . .
RUN npm ci --omit=dev
EXPOSE 8080
CMD ["node", "server.js"]
```
Your server MUST bind `0.0.0.0:8080` - the internal port `compose.demo.yml` publishes as
`$PORT`. A port mismatch or a missing COPY is the #1 cause of a dead demo.
"""

_SCAFFOLD_REPORT = """

## Worked example - serve your report as a static site (keep this shape)
Render your report to HTML, then serve it with a static web server:

`compose.base.yml`
```yaml
services:
  web:
    build: .
```
`compose.demo.yml`
```yaml
services:
  web:
    ports:
      - "${PORT}:80"
```
`Dockerfile`
```dockerfile
FROM nginx:alpine
COPY report/ /usr/share/nginx/html/
```
Put your rendered report at `report/index.html` (a summary, findings tables, sections,
CSS). nginx serves it on port 80, which `compose.demo.yml` publishes as `$PORT`. No
application logic is needed - the report content IS the deliverable.
"""


def one_shot_example(project) -> str:
    """A worked example of this project's deliverable boot contract, injected into the
    build task as a demonstration (§Phase 2 one-shot lever)."""
    dt = deliverable_type(project)
    if dt == "pull_request":
        return ""  # no boot contract - a demo one-shot would teach the wrong shape
    return _SCAFFOLD_REPORT if dt in _REPORT_TYPES else _SCAFFOLD_DEMO



# §deliverable-aware prompt: the demo/compose obligations below are the DEFAULT
# build contract, and they used to be unconditional - the deliverable override
# reached only line 3 of the prompt while "Non-negotiable rules", the working
# method's verify step and the closing Deliverable all went on demanding a
# bootable MVP. A pull-request run therefore read four separate orders to ship
# compose files and boot the stack, and obeyed them: one production check that
# only had to compare prices against public docs spent its run on `make env` and
# `docker compose up -d --build`. Non-PR deliverables (deployed_demo and the
# report tracks, which ARE demo-booted) render byte-identical text to before.
_APP_CONTRACT_RULES = """1. **OCPA compose convention** (base file + per-env overlays, secrets only via env vars): compose.base.yml +
   compose.dev.yml + compose.prod.yml + Makefile + .env.example with NO defaults for
   secrets (crash if unset). `main` is staging. All work happens through merge requests;
   green OCPA CI auto-merges. Never push to `main` directly.
   You MUST also ship a **`.gitlab-ci.yml`** at the repo root that validates the build.
   It must run on a plain docker-executor runner (no privileged dind) - use exactly this
   shape, extended with any extra static checks that make sense, but keep the
   `validate-compose` job so the demo contract is enforced:

   ```yaml
   stages: [validate]
   validate-compose:
     stage: validate
     image: docker:27
     variables:
       PORT: "8080"
     script:
       - docker compose -f compose.base.yml -f compose.demo.yml config
   ```
2. **Demo routing contract**: place all project files at the REPOSITORY ROOT (do NOT
   nest them inside a subdirectory). The repo MUST ship `compose.demo.yml` at the root,
   exposing EXACTLY ONE HTTP service, published on the injected `$PORT` environment
   variable (`ports: "${PORT}:<internal>"`). Internal services (db, cache, workers) stay
   on the compose network and are never published.
   **The image must be self-contained and actually boot.** Every file the container
   needs at runtime is baked into the image: a Dockerfile that starts `node server.js`
   MUST `COPY server.js` (prefer `COPY . .` plus a `.dockerignore` over listing files -
   forgetting one file is the #1 cause of a dead demo). Match the port your server
   listens on to the internal port `compose.demo.yml` publishes. Before finishing,
   re-read your Dockerfile and verify every runtime file is copied and the entrypoint
   exists in the image. The platform test-boots
   `docker compose -f compose.base.yml -f compose.demo.yml up -d --build` after your
   push and the exposed service must answer HTTP - a build that does not come up is
   bounced back to you (billed) or fails the delivery, so treat a booting demo as part
   of the deliverable, not an afterthought."""

_PR_CONTRACT_RULES = """1. **The repository's conventions win.** You are changing an EXISTING repository:
   follow its layout, tooling, dependency management and CI as they already are. Do
   not introduce a compose triplet, a Makefile, an `.env.example` or a `.gitlab-ci.yml`
   because this platform likes them - if the repo has none, it wants none.
2. **No platform scaffolding.** There is no demo routing contract on this run: no
   `compose.demo.yml`, no `$PORT` service, no Dockerfile or server stub added to make
   the repository "bootable" here. Nothing test-boots this deliverable after you push;
   the reviewer reads the diff. Files whose only purpose is to satisfy the platform are
   noise in that diff."""

_APP_VERIFY_WORKFLOW = """This sandbox runs its OWN docker daemon (`docker info` confirms it): a repository that ships a container workflow - compose files, a Makefile with dev/prod targets, a devcontainer - is built, run and tested through THAT workflow (`make dev`, `docker compose up --build`, its documented commands), because its containers already carry every toolchain it needs."""

_PR_VERIFY_WORKFLOW = (
    "Use the repository's own checks - its test suite, linters and CI config. This "
    "sandbox has a docker daemon, but booting the project is NOT part of finishing: "
    "do it only when the change you made cannot be verified any other way, never as a "
    "routine step, and never on a task that asks you to inspect or report rather than "
    "to change behaviour."
)

_APP_REVERIFY_NOTE = """The platform re-verifies with its own boot check; a build that fails its own stated check is not done."""

_PR_REVERIFY_NOTE = (
    "There is no platform boot check on this run - your own verification is the only "
    "one, so state in your summary what you actually ran."
)

_APP_DELIVERABLE_SUMMARY = """A working MVP matching the project description and onboarding answers, deployable with
`docker compose -f compose.base.yml -f compose.demo.yml up -d` with `$PORT` injected,
with a passing OCPA CI pipeline and a concise README."""

_PR_DELIVERABLE_SUMMARY = (
    "The requested change on the customer's existing repository, as a minimal reviewable\n"
    "diff - or, when the honest answer is that nothing needs changing, the report and the\n"
    "`no_change_needed` declaration of working-method steps 8 and 9."
)

_APP_ROLE = "building a customer MVP."
_PR_ROLE = "working on a customer's EXISTING repository."


def prompt_overlays(project) -> dict:
    """Deliverable-dependent substitutions for `development_system.md`.

    ONE entry point so the two variants of each block stay in sync. Only
    `pull_request` diverges: everything else is demo-booted by the platform and
    renders exactly what it always did.
    """
    pr = deliverable_type(project) == "pull_request"
    return {
        "AGENT_ROLE": _PR_ROLE if pr else _APP_ROLE,
        "PLATFORM_CONTRACT_RULES": _PR_CONTRACT_RULES if pr else _APP_CONTRACT_RULES,
        "VERIFY_WORKFLOW": _PR_VERIFY_WORKFLOW if pr else _APP_VERIFY_WORKFLOW,
        "REVERIFY_NOTE": _PR_REVERIFY_NOTE if pr else _APP_REVERIFY_NOTE,
        "DELIVERABLE_SUMMARY": _PR_DELIVERABLE_SUMMARY if pr else _APP_DELIVERABLE_SUMMARY,
    }
