"""Deterministic pre-boot demo-contract linter (§Phase 1).

Contract/format violations are the #1 dev-harness failure class in the literature
(~36%). This catches the common ones in <1s, BEFORE the 60-1500s throwaway-sandbox
boot gate spins a DinD up only to 502. It is a pre-check, not a replacement: it
fails ONLY when it is certain the platform contract is violated (compose.demo.yml
unparseable, or no service publishes the injected $PORT) and passes on any
ambiguity, so the boot gate stays the real arbiter. It never raises - an internal
error fails OPEN to the boot gate.

Platform contract (mirrors deployer/main.py + the scaffold): a required
`compose.demo.yml` (repo root or one subdir) with, merged over the optional
`compose.base.yml`, at least one service publishing the injected `$PORT`
(`ports: ["${PORT}:8080"]`); the app must listen on that internal port.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

# The deployer injects PORT (uppercase) as an env var; docker compose substitution
# is case-sensitive, so only $PORT / ${PORT} bind it. A lowercase ${port} would not
# substitute and SHOULD fail the lint. \b keeps $PORTFOLIO etc. from matching.
_PORT_RE = re.compile(r"\$\{?PORT\b")


def _publishes_port(compose: dict | None) -> bool:
    """True if any service in this compose doc publishes $PORT (short-form
    "${PORT}:8080" or long-form {published: ${PORT}, target: 8080})."""
    services = (compose or {}).get("services")
    if not isinstance(services, dict):
        return False
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        for entry in svc.get("ports") or []:
            if isinstance(entry, str):
                text = entry
            elif isinstance(entry, dict):
                text = str(entry.get("published", ""))
            else:
                text = ""
            if _PORT_RE.search(text):
                return True
    return False


def _has_alt_bind_path(compose: dict | None) -> bool:
    """True if any service could publish $PORT by a route this static check can't
    see: `network_mode: host` (binds directly in the DinD host netns, no `ports:`
    line needed) or `extends:` (ports inherited from a file we don't read). When
    present we must NOT reject on 'no $PORT publish' - defer to the boot gate."""
    services = (compose or {}).get("services")
    if not isinstance(services, dict):
        return False
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        if svc.get("network_mode") == "host" or "extends" in svc:
            return True
    return False


def check_demo_contract(demo_dir: Path) -> tuple[bool, str]:
    """(ok, fix_message) for the demo dir holding compose.demo.yml. ok=True passes
    the run through to the boot gate; ok=False is a deterministic contract failure
    the agent can fix (fed back through the existing boot-fix loop). Fails open."""
    try:
        demo = demo_dir / "compose.demo.yml"
        if not demo.is_file():
            return True, ""  # a missing file is the boot gate's message, not the lint's

        try:
            demo_doc = yaml.safe_load(demo.read_text()) or {}
        except yaml.YAMLError as exc:
            return False, (
                "compose.demo.yml is not valid YAML, so `docker compose` will reject it "
                f"before the demo can start: {str(exc)[:200]}. Fix the YAML and push again.")

        base_doc: dict = {}
        base = demo_dir / "compose.base.yml"
        if base.is_file():
            try:
                base_doc = yaml.safe_load(base.read_text()) or {}
            except yaml.YAMLError:
                base_doc = {}  # a broken base is the boot gate's problem, not the lint's

        if _publishes_port(demo_doc) or _publishes_port(base_doc):
            return True, ""

        # A service may bind $PORT without a visible `ports:` publish (host netns,
        # or ports inherited via extends). We can't prove it statically, so defer
        # to the boot gate rather than risk rejecting a build that boots fine.
        if _has_alt_bind_path(demo_doc) or _has_alt_bind_path(base_doc):
            return True, ""

        return False, (
            "No service publishes the injected $PORT, so the platform health probe "
            "(http://localhost:$PORT) can never reach your app and the demo will 502. "
            "The platform runs `docker compose -f compose.base.yml -f compose.demo.yml up` "
            "with $PORT injected as an environment variable - publish it on your web "
            "service and make the app listen on that internal port, e.g. in "
            "compose.demo.yml:\n\n"
            "  services:\n    web:\n      ports:\n        - \"${PORT}:8080\"\n")
    except Exception:
        # Defence-in-depth: the linter is a cheap optimisation and must NEVER break a
        # build. On any unexpected error, pass through to the real boot gate.
        return True, ""
