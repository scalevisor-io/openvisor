"""§deliverable-aware prompt: a pull-request run is not told to build an MVP.

`development_system.md` is written for the default deployed-demo build, and its
demo obligations used to be unconditional - `{{DELIVERABLE_CLAUSE}}` reaches only
line 3, while the "Non-negotiable rules", the working method's verify step and
the closing Deliverable all went on demanding a bootable stack. A pull-request
run therefore read four separate orders to ship compose files and boot the
project, and obeyed them: a production check whose whole job was to compare
prices against public documentation spent its run on `make env` and
`docker compose up -d --build`.

The safety property these pin: for every NON-pull_request deliverable the
rendered prompt is byte-identical to what it was before the split, because those
runs really are demo-booted by the platform.
"""
from types import SimpleNamespace

import pytest

from app.services import speciality

# The IMPERATIVES, not the words: the pull-request clause legitimately names
# compose.demo.yml in a prohibition, so asserting on bare filenames would fail on
# correct text and pass on nothing useful.
DEMO_ORDERS = [
    "The repo MUST ship `compose.demo.yml`",
    "You MUST also ship a **`.gitlab-ci.yml`**",
    "The platform test-boots",
    "docker compose up -d --build",
    "building a customer MVP",
    "A working MVP matching the project description",
    "place all project files at the REPOSITORY ROOT",
]


def _project(kind="ai", spec="general-webapp"):
    return SimpleNamespace(kind=kind, speciality=spec)


def _render(project):
    """The system prompt exactly as _build_task_file assembles it."""
    from app.agents.pipeline import load_prompt
    text = (load_prompt("development_system.md")
            .replace("{{DELIVERABLE_CLAUSE}}", speciality.deliverable_clause(project))
            .replace("{{SOVEREIGN_CLAUSE}}", "x")
            .replace("{{FORBIDDEN_ACTIONS_JSON}}", "[]"))
    for key, value in speciality.prompt_overlays(project).items():
        text = text.replace("{{" + key + "}}", value)
    return text


def test_a_pull_request_run_is_never_told_to_ship_a_demo():
    text = _render(_project(kind="auto_dev"))
    for order in DEMO_ORDERS:
        assert order not in text, f"PR run still told: {order}"


def test_a_pull_request_run_is_not_told_to_boot_the_stack_routinely():
    text = _render(_project(kind="auto_dev"))
    assert "booting the project is NOT part of finishing" in text
    assert "no platform boot check on this run" in text


def test_a_demo_run_keeps_every_demo_obligation():
    """The default path must not lose anything: these runs ARE test-booted."""
    text = _render(_project(kind="ai"))
    for order in DEMO_ORDERS:
        assert order in text, f"demo run lost: {order}"
    assert "Demo routing contract" in text
    assert "The platform test-boots" in text


@pytest.mark.parametrize("kind,spec", [("ai", "general-webapp"), ("auto_dev", "general-webapp")])
def test_no_placeholder_survives_rendering(kind, spec):
    """An unrendered {{TOKEN}} would reach the agent as literal noise."""
    text = _render(_project(kind=kind, spec=spec))
    assert "{{" not in text and "}}" not in text


def test_overlays_cover_every_placeholder_the_prompt_declares():
    """A placeholder added to the prompt without a variant fails here, not in prod."""
    import re
    from app.agents.pipeline import load_prompt
    declared = set(re.findall(r"\{\{([A-Z_]+)\}\}", load_prompt("development_system.md")))
    rendered_elsewhere = {"BRAND_NAME", "CONSULTANT_NAME", "CONSULTANT_FIRST_NAME",
                          "CONSULTANT_FOCUS", "DEPLOY_DOMAIN", "DELIVERABLE_CLAUSE",
                          "SOVEREIGN_CLAUSE", "FORBIDDEN_ACTIONS_JSON"}
    assert declared - rendered_elsewhere == set(speciality.prompt_overlays(_project()))
