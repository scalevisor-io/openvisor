"""§dev harness: which agent driver a project's dev build runs inside the sandbox.

The catalog is a CODE constant, not admin data. Every harness is a driver script
baked into the runner image (`runner/`), so an id the image has no driver for is
unrunnable and an instance cannot invent one. What IS admin-editable are the three
AppSetting keys below: whether per-project selection is offered at all (off by
default), which harnesses the instance
permits, and the instance-wide default.

`resolve()` is the single source of truth, and it is fail-closed in the direction
that matters: with selection disabled a stored `Project.dev_harness` is IGNORED,
not merely hidden in the UI, so switching the feature off actually stops projects
building on a non-default harness. A project's choice only ever NARROWS the
admin's allowed set, exactly like the §KB per-project selection.

The harness id is deliberately NOT hashed into the run fingerprint on its own: it
rides in `tool_preset_id`, which `agent_eval/harness_version.py` already folds in.
Two consequences, both intended - every registered harness MUST carry a distinct
preset id (that string is what makes two harnesses incomparable), and introducing
this module changed no existing fingerprint, so runs recorded before it stay
comparable with runs after it.
"""
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import Project
from app.services import app_settings
from app.services.agent_eval.harness_version import (
    DRIVER_REVISION as _OPENHANDS_DRIVER,
    TOOL_PRESET_ID as _OPENHANDS_PRESET,
)


@dataclass(frozen=True)
class Harness:
    id: str
    label: str
    description: str
    driver: str  # absolute path of the driver script INSIDE the runner image
    tool_preset_id: str  # which tools the agent gets; distinct per harness
    # Which implementation hands them over, including the pinned agent-SDK version.
    # Hashed into the fingerprint separately from the preset because a driver change
    # can move cost by multiples without touching a tool, a prompt or a cap - the
    # Anthropic prompt-caching fix cut a build's input bill ~4x and left the preset
    # untouched. Bump on ANY change to the driver script or its pinned dependency.
    driver_revision: str
    # Substrings that mark a model name this driver can actually drive; empty = any.
    # The OpenHands driver speaks the OpenAI-compatible API and runs whatever name
    # the endpoint serves. The Claude driver runs the `claude` CLI, which checks the
    # name against its OWN model list BEFORE it ever reaches the gateway, so a
    # foreign name ends the build in seconds with `unrecognized_model` - seen in
    # production on 2026-08-30, where a project pinned to this harness while its
    # endpoint served qwen3.6-35b-a3b spent a sandbox to learn it. Deliberately
    # generous: a gateway may serve Claude under its own spelling, and the driver's
    # own error still catches a name that is genuinely wrong. This is a
    # COMPATIBILITY test, not an allowlist.
    model_hints: tuple[str, ...] = ()


DEFAULT_ID = "openhands"

HARNESSES: dict[str, Harness] = {
    "openhands": Harness(
        id="openhands",
        label="OpenHands SDK",
        description="The default agent loop: the OpenHands SDK v1 driver.",
        driver="/run_dev.py",
        tool_preset_id=_OPENHANDS_PRESET,
        driver_revision=_OPENHANDS_DRIVER,
    ),
    "claude_sdk": Harness(
        id="claude_sdk",
        label="Claude Agent SDK",
        description="The Claude Code agent loop. Anthropic models only.",
        driver="/run_claude.py",
        # Distinct from the OpenHands preset, which is what stops agent_eval from
        # comparing the two as one harness.
        tool_preset_id=(
            "claude-sdk:builtin-minus-interactive"
            "(read+write+edit+bash+glob+grep+websearch+task)"),
        # Both halves of this harness are the agent: the SDK and the CLI it drives
        # as a subprocess, pinned together in runner/Dockerfile.
        driver_revision="claude-sdk0.2.148+cli2.1.251+drv4",
        model_hints=("claude", "anthropic", "sonnet", "opus", "haiku"),
    ),
}

# AppSetting keys (runtime, admin-editable - core/config.py stays env-only).
SELECTION_ENABLED = "dev_harness_selection_enabled"
ALLOWED_KEY = "dev_harness_allowed"
DEFAULT_KEY = "dev_harness_default"


def normalize_ids(raw, fallback: list[str]) -> list[str]:
    """Registered ids only, deduped, order preserved. Anything unusable (a wrong
    type, an id from a release that dropped the driver, an empty list) falls back,
    so a bad row can never leave the instance with no runnable harness."""
    if not isinstance(raw, list):
        return list(fallback)
    cleaned: list[str] = []
    for value in raw:
        if isinstance(value, str) and value in HARNESSES and value not in cleaned:
            cleaned.append(value)
    return cleaned or list(fallback)


def _default_id(stored) -> str:
    return stored if (isinstance(stored, str) and stored in HARNESSES) else DEFAULT_ID


def model_supported(harness: Harness, model: str | None) -> bool:
    """Can this harness's driver actually drive `model`?

    A harness with no `model_hints` takes anything. One with hints takes a name
    that carries any of them, matched on the bare model id (a provider-prefixed
    routing name like `anthropic/claude-sonnet-5` is normalised the way both
    drivers normalise it for billing). An EMPTY model is not a verdict - nothing
    is known, so nothing is refused.
    """
    if not harness.model_hints:
        return True
    name = (model or "").split("/")[-1].strip().lower()
    if not name:
        return True
    return any(hint in name for hint in harness.model_hints)


def _runnable(harness: Harness, default: Harness, model: str | None) -> Harness:
    """Degrade a harness that cannot run this project's model.

    Same shape as the withdrawn-pin path: a build gets made rather than failed,
    and the fingerprint records the harness that actually ran. The built-in
    default is the last resort because it carries no model hints, so it can
    always run - an instance whose DEFAULT is model-restricted must not strand
    every project pointed at another model.
    """
    if model_supported(harness, model):
        return harness
    if harness is not default and model_supported(default, model):
        return default
    return HARNESSES[DEFAULT_ID]


# ---- sync accessors (Celery workers) ----

def selection_enabled(db: Session) -> bool:
    return bool(app_settings.get_setting_sync(db, SELECTION_ENABLED, False))


def allowed_ids(db: Session) -> list[str]:
    """Which harnesses this instance permits. Defaults to the default one ALONE:
    a harness shipped by a later release has to be opted into by an admin, never
    inherited by an instance that once ticked "allow selection"."""
    return normalize_ids(app_settings.get_setting_sync(db, ALLOWED_KEY, None), [DEFAULT_ID])


def instance_default(db: Session) -> Harness:
    return HARNESSES[_default_id(app_settings.get_setting_sync(db, DEFAULT_KEY, None))]


def resolve(db: Session, project: Project, model: str | None = None) -> Harness:
    """The harness this project's next dispatch executes.

    Precedence: the project's pin (only while selection is enabled AND the id is
    still registered and still allowed), else the instance default, else the
    built-in default. An id that was allowed when it was pinned and has since been
    withdrawn degrades to the default rather than failing the build - the same
    way a deleted ModelEndpoint degrades to the global model (services/
    model_config.py).

    `model` is the name this project's calls run on. Pass it and a harness that
    cannot drive that model degrades the same way, because the alternative is a
    sandbox that spins up, clones the repo and dies on the first model call - what
    production did on 2026-08-30. Callers that do not know the model (an admin
    preview, a test) omit it and get the pin as stored."""
    default = instance_default(db)
    if not selection_enabled(db):
        return _runnable(default, default, model)
    chosen = (project.dev_harness or "").strip()
    if chosen and chosen in HARNESSES and chosen in allowed_ids(db):
        return _runnable(HARNESSES[chosen], default, model)
    return _runnable(default, default, model)


# ---- async accessors (API routes) ----

async def admin_state(db: AsyncSession) -> dict:
    """The dev-harness block of the admin settings payload: the flag, the allowed
    set, the instance default, and the catalog this image can actually run."""
    return {
        "dev_harness_selection_enabled": await app_settings.get_flag(db, SELECTION_ENABLED),
        "dev_harness_allowed": normalize_ids(
            await app_settings.get_value(db, ALLOWED_KEY, None), [DEFAULT_ID]),
        "dev_harness_default": _default_id(await app_settings.get_value(db, DEFAULT_KEY, None)),
        "dev_harnesses": [{"id": h.id, "label": h.label, "description": h.description}
                          for h in HARNESSES.values()],
    }


async def selectable_ids(db: AsyncSession) -> list[str]:
    """Ids a project may be pinned to right now. EMPTY while selection is disabled:
    resolve() would ignore a stored pin, so the route refuses to store one rather
    than record a preference that silently does nothing."""
    if not await app_settings.get_flag(db, SELECTION_ENABLED):
        return []
    return normalize_ids(await app_settings.get_value(db, ALLOWED_KEY, None), [DEFAULT_ID])
