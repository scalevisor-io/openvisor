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

