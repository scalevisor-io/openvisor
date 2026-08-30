"""§dev harness: per-project agent-driver selection, its admin gate and its plumbing.

The invariant this file exists for: with selection DISABLED, a stored per-project
pin must be ignored rather than merely hidden, so switching the feature off really
does move every project back onto the instance default. The rest covers the
resolver's precedence, the fingerprint separation that keeps two harnesses from
being compared with each other, the admin GET/PUT contract, and the id reaching the
sandbox (deployer env + the runner's driver dispatch).
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.config import settings
from app.core.db import SyncSession
from app.core.deps import require_admin
from app.main import app
from app.models import AppSetting, Organization, Project
from app.services import dev_harness
from app.services.agent_eval.harness_version import compute_harness_version

KEYS = [dev_harness.SELECTION_ENABLED, dev_harness.ALLOWED_KEY, dev_harness.DEFAULT_KEY]

# Sibling sources mounted read-only into the api container for tests that pin a
# cross-service contract (the convention test_sandbox_git_preflight follows).
RUNNER_ENTRYPOINT = Path("/app/runner_src/entrypoint.sh")
DEPLOYER_MAIN = Path("/app/deployer_src/main.py")
DEPLOYER_K8S = Path("/app/deployer_src/k8s.py")


@pytest.fixture(autouse=True)
def _clean_harness_settings():
    """Clean on BOTH sides: these are instance-wide rows, so a developer who
    enabled the feature on their own stack would otherwise fail the default-posture
    assertions with state the suite never wrote."""
    def wipe():
        with SyncSession() as db:
            db.execute(delete(AppSetting).where(AppSetting.key.in_(KEYS)))
            db.commit()
    wipe()
    yield
    wipe()


@pytest.fixture
def second_harness(monkeypatch):
    """A second registered harness, so the multi-harness paths are exercised while
    the image really ships one driver. Same shape as a real registry entry."""
    extra = dev_harness.Harness(id="probe", label="Probe", description="test-only driver",
                                driver="/run_probe.py", tool_preset_id="probe:terminal",
                                driver_revision="probe-drv1")
    monkeypatch.setitem(dev_harness.HARNESSES, "probe", extra)
    return extra


def _settings(db, *, enabled=None, allowed=None, default=None):
    if enabled is not None:
        db.add(AppSetting(key=dev_harness.SELECTION_ENABLED, value=enabled))
    if allowed is not None:
        db.add(AppSetting(key=dev_harness.ALLOWED_KEY, value=allowed))
    if default is not None:
        db.add(AppSetting(key=dev_harness.DEFAULT_KEY, value=default))
    db.commit()


def _project(db, harness=None):
    org = Organization(name="Harness Test Org", credit_balance=10.0)
    db.add(org)
    db.flush()
    p = Project(org_id=org.id, name="P", description="d", kind="ai",
                status="development", dev_harness=harness)
    db.add(p)
    db.flush()
    return p


# ------------------------------------------------------------------ default posture

def test_selection_is_off_and_the_default_is_openhands():
    with SyncSession() as db:
        assert dev_harness.selection_enabled(db) is False
        assert dev_harness.allowed_ids(db) == [dev_harness.DEFAULT_ID]
        assert dev_harness.instance_default(db).id == "openhands"


def test_every_registered_harness_has_a_distinct_tool_preset():
    """The preset id IS the fingerprint discriminator - two harnesses sharing one
    would be silently compared against each other by agent_eval."""
    presets = [h.tool_preset_id for h in dev_harness.HARNESSES.values()]
    assert len(presets) == len(set(presets))
    revisions = [h.driver_revision for h in dev_harness.HARNESSES.values()]
    assert len(revisions) == len(set(revisions))


# ------------------------------------------------------------------------- resolve

def test_disabled_selection_ignores_a_stored_pin(second_harness):
    """The licensing invariant: turning the feature off is not cosmetic."""
    with SyncSession() as db:
        _settings(db, enabled=False, allowed=["openhands", "probe"])
        p = _project(db, harness="probe")
        assert dev_harness.resolve(db, p).id == "openhands"
        db.rollback()


def test_enabled_selection_honors_an_allowed_pin(second_harness):
    with SyncSession() as db:
        _settings(db, enabled=True, allowed=["openhands", "probe"])
        p = _project(db, harness="probe")
        assert dev_harness.resolve(db, p).id == "probe"
        db.rollback()


def test_a_withdrawn_pin_degrades_to_the_instance_default(second_harness):
    """Allowed when it was pinned, withdrawn since: build on the default rather
    than fail, the way a deleted ModelEndpoint degrades to the global model."""
    with SyncSession() as db:
        _settings(db, enabled=True, allowed=["openhands"])
        p = _project(db, harness="probe")
        assert dev_harness.resolve(db, p).id == "openhands"
        db.rollback()


def test_an_unknown_pin_degrades_to_the_instance_default():
    with SyncSession() as db:
        _settings(db, enabled=True)
        p = _project(db, harness="from-a-release-that-dropped-it")
        assert dev_harness.resolve(db, p).id == "openhands"
        db.rollback()


def test_no_pin_follows_the_instance_default(second_harness):
    with SyncSession() as db:
        _settings(db, enabled=True, allowed=["openhands", "probe"], default="probe")
        p = _project(db)
        assert dev_harness.resolve(db, p).id == "probe"
        db.rollback()


def test_allowed_ids_drops_junk_and_never_empties():
    with SyncSession() as db:
        _settings(db, allowed=["nope", 7, None])
        assert dev_harness.allowed_ids(db) == [dev_harness.DEFAULT_ID]


# --------------------------------------------------------------------- fingerprint

def _hv(h):
    return compute_harness_version(settings, tool_preset_id=h.tool_preset_id,
                                   driver_revision=h.driver_revision)


def test_harnesses_fingerprint_differently(second_harness):
    openhands = _hv(dev_harness.HARNESSES["openhands"])
    probe = _hv(second_harness)
    assert openhands != probe
    assert openhands == compute_harness_version(settings)  # the default is unchanged


def test_a_driver_change_moves_the_fingerprint(second_harness):
    """The reason this field exists: enabling Anthropic prompt caching cut a build's
    input bill by roughly 4x while touching no tool, prompt or cap, and produced a
    byte-identical fingerprint - so cheap runs and expensive ones aggregated as one
    harness. Only driver_revision differs between these two."""
    import dataclasses
    bumped = dataclasses.replace(second_harness, driver_revision="probe-drv2")
    assert _hv(second_harness) != _hv(bumped)
    assert second_harness.tool_preset_id == bumped.tool_preset_id  # nothing else moved


def test_stamp_uses_the_resolved_harness(second_harness):
    from app.workers import tasks
    with SyncSession() as db:
        _settings(db, enabled=True, allowed=["openhands", "probe"])
        p = _project(db, harness="probe")
        tasks._stamp_harness_version(db, p)
        assert p.dev_harness_version == _hv(second_harness)
        db.rollback()


# ------------------------------------------------------------- model compatibility

def test_a_harness_without_hints_runs_any_model():
    """The OpenHands driver speaks the OpenAI-compatible API: whatever name the
    endpoint serves is the model, and nothing here may narrow that."""
    openhands = dev_harness.HARNESSES["openhands"]
    assert openhands.model_hints == ()
    for model in ("qwen3.6-35b-a3b", "gpt-5.6-terra", "claude-sonnet-5", "", None):
        assert dev_harness.model_supported(openhands, model)


@pytest.mark.parametrize("model", [
    "claude-sonnet-5", "anthropic/claude-opus-5", "Claude-Haiku-4-5", "sonnet",
    "us.anthropic.claude-sonnet-5-v1:0",
])
def test_the_claude_harness_accepts_every_spelling_of_a_claude_model(model):
    """Generous on purpose: a gateway may serve Claude under its own name, and the
    driver's own error still catches a name that is genuinely wrong."""
    assert dev_harness.model_supported(dev_harness.HARNESSES["claude_sdk"], model)


@pytest.mark.parametrize("model", ["qwen3.6-35b-a3b", "gpt-5.6-terra",
                                   "deepseek-v4-flash-0731", "mistral-medium-3.5"])
def test_the_claude_harness_refuses_a_foreign_model(model):
    """Production, 2026-08-30: a project pinned to claude_sdk while its endpoint
    served qwen3.6-35b-a3b spun a sandbox up, cloned the repo and died on the first
    model call - the `claude` CLI checks the name against its OWN list before it
    ever reaches the gateway (`unrecognized_model`)."""
    assert not dev_harness.model_supported(dev_harness.HARNESSES["claude_sdk"], model)


def test_an_unknown_model_is_not_a_verdict():
    """Callers that do not know the model (an admin preview, a test) must get the
    pin as stored rather than a silent downgrade."""
    assert dev_harness.model_supported(dev_harness.HARNESSES["claude_sdk"], None)


def test_a_pin_the_model_cannot_run_degrades_to_the_default():
    with SyncSession() as db:
        _settings(db, enabled=True, allowed=["openhands", "claude_sdk"])
        p = _project(db, harness="claude_sdk")
        assert dev_harness.resolve(db, p).id == "claude_sdk"           # no model: as pinned
        assert dev_harness.resolve(db, p, model="claude-sonnet-5").id == "claude_sdk"
        assert dev_harness.resolve(db, p, model="qwen3.6-35b-a3b").id == "openhands"
        db.rollback()


def test_an_instance_default_the_model_cannot_run_degrades_too():
    """The built-in default is the last resort: an instance whose DEFAULT is
    model-restricted must not strand every project pointed at another model."""
    with SyncSession() as db:
        _settings(db, enabled=True, allowed=["openhands", "claude_sdk"],
                  default="claude_sdk")
        p = _project(db)
        assert dev_harness.resolve(db, p, model="claude-opus-5").id == "claude_sdk"
        assert dev_harness.resolve(db, p, model="gpt-5.6-terra").id == "openhands"
        db.rollback()


def test_a_disabled_selection_still_degrades_an_incompatible_default():
    with SyncSession() as db:
        _settings(db, enabled=False, default="claude_sdk")
        p = _project(db)
        assert dev_harness.resolve(db, p, model="qwen3.6-35b-a3b").id == "openhands"
        db.rollback()


def test_the_stamp_follows_the_harness_that_can_actually_run(monkeypatch):
    """A stamp that ignored the degrade would file the run under a harness that
    never executed, and the benchmark would compare it with real Claude builds."""
    from app.services import model_config
    from app.workers import tasks
    monkeypatch.setattr(model_config, "project_model_name",
                        lambda db, project: "qwen3.6-35b-a3b")
    with SyncSession() as db:
        _settings(db, enabled=True, allowed=["openhands", "claude_sdk"])
        p = _project(db, harness="claude_sdk")
        tasks._stamp_harness_version(db, p)
        assert p.dev_harness_version == _hv(dev_harness.HARNESSES["openhands"])
        db.rollback()


# ------------------------------------------------------------------------ plumbing

def test_deployer_client_forwards_the_harness(monkeypatch):
    from app.services import deployer_client
    sent = {}
    monkeypatch.setattr(deployer_client, "_call",
                        lambda method, path, body, timeout=None: sent.update(body) or {})
    deployer_client.run_dev_job("pid", llm_model="m", llm_api_key="k", llm_base_url="u",
                                harness="probe")
    assert sent["harness"] == "probe"


def test_the_deployer_hands_the_harness_to_the_sandbox():
    """Source-level, like test_sandbox_git_preflight: both orchestrator paths put
    the resolved id in the sandbox's environment."""
    if not DEPLOYER_MAIN.exists():
        pytest.skip("deployer source not mounted at /app/deployer_src")
    assert 'f"DEV_HARNESS={body.harness}"' in DEPLOYER_MAIN.read_text()
    assert '{"name": "DEV_HARNESS", "value": body.harness}' in DEPLOYER_K8S.read_text()


def test_the_spend_ceiling_reaches_the_sandbox(monkeypatch):
    """The Claude driver reads DEV_RUN_MAX_USD as its per-run provider-side budget.
    It shipped with nothing setting the variable, so the ceiling documented in
    CODE_MAP could never fire - the run loop simply saw 0 and passed None."""
    from app.services import deployer_client
    sent = {}
    monkeypatch.setattr(deployer_client, "_call",
                        lambda method, path, body, timeout=None: sent.update(body) or {})
    deployer_client.run_dev_job("pid", llm_model="m", llm_api_key="k", llm_base_url="u",
                                max_usd=7.5)
    assert sent["max_usd"] == 7.5
    if not DEPLOYER_MAIN.exists():
        pytest.skip("deployer source not mounted at /app/deployer_src")
    assert "DEV_RUN_MAX_USD={body.max_usd" in DEPLOYER_MAIN.read_text()
    assert '{"name": "DEV_RUN_MAX_USD"' in DEPLOYER_K8S.read_text()
    if RUN_CLAUDE.exists():
        assert 'os.environ.get("DEV_RUN_MAX_USD")' in RUN_CLAUDE.read_text()


def test_the_entrypoint_dispatches_on_the_harness_and_falls_back():
    """An id this image ships no driver for must build on the default, never abort:
    a newer platform dispatching to an older runner still has to produce a build."""
    if not RUNNER_ENTRYPOINT.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    entry = RUNNER_ENTRYPOINT.read_text()
    assert 'case "${DEV_HARNESS:-openhands}"' in entry
    assert 'python -u "$DRIVER"' in entry
    assert entry.index("unknown DEV_HARNESS") < entry.index('python -u "$DRIVER"')
    assert entry.index("is not in this image") < entry.index('python -u "$DRIVER"')


# ------------------------------------------------------- the second driver's contract

RUN_CLAUDE = Path("/app/runner_src/run_claude.py")
RUNNER_DOCKERFILE = Path("/app/runner_src/Dockerfile")


def test_the_entrypoint_maps_every_registered_harness_to_a_driver():
    """A registered id the image cannot dispatch is a build that silently runs on
    the wrong agent - the fallback hides it, so pin the mapping here instead."""
    if not RUNNER_ENTRYPOINT.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    entry = RUNNER_ENTRYPOINT.read_text()
    for h in dev_harness.HARNESSES.values():
        assert f"{h.id}) DRIVER=\"{h.driver}\"" in entry, h.id


def test_the_claude_driver_reproduces_the_openvisor_artifact_contract():
    """The worker reads a build's outcome off .openvisor/ and cannot tell which
    harness wrote it. A driver that skips one of these does not fail loudly - it
    bills nothing, or narrates nothing, or loses the plan gate."""
    if not RUN_CLAUDE.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    src = RUN_CLAUDE.read_text()
    for artifact in ("usage.json", "error.json", "exit_reason.json", "task.md",
                     "steering.md", "mcp.json"):
        assert artifact in src, artifact
    # usage.json must be written atomically or a kill mid-write leaves a torn file
    assert "os.replace(tmp, path)" in src
    # every input token the provider charges for has to reach the meter
    assert "cache_creation_input_tokens" in src and "cache_read_input_tokens" in src
    # the run must be billed even when it errors or hits the cap
    assert "finally:" in src and "_dump_usage(usage)" in src


def test_the_claude_driver_never_asks_for_permission_as_root():
    """bypassPermissions maps to the CLI's --dangerously-skip-permissions, which
    refuses to run as root - and the runner IS root. Regressing to it turns every
    Claude build into an immediate exit 1."""
    if not RUN_CLAUDE.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    src = RUN_CLAUDE.read_text()
    assert 'permission_mode="bypassPermissions"' not in src
    assert "can_use_tool=_approve" in src and "PermissionResultAllow()" in src


def test_the_claude_driver_does_not_load_customer_repo_settings():
    """/workspace is the CUSTOMER's repository. Letting the SDK read its .claude/
    would let a customer repo inject hooks and skills into a build running with
    platform credentials."""
    if not RUN_CLAUDE.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    assert "setting_sources=[]" in RUN_CLAUDE.read_text()


def _run_claude_module():
    """Import the driver itself, so the MCP translation below is exercised rather
    than pattern-matched. The Agent SDK is imported inside the run loop, not at
    module level, so this works in the api container."""
    import importlib.util
    import sys
    if str(RUN_CLAUDE.parent) not in sys.path:
        sys.path.insert(0, str(RUN_CLAUDE.parent))  # for its `import live_events`
    spec = importlib.util.spec_from_file_location("run_claude_under_test", RUN_CLAUDE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_claude_driver_types_every_staged_mcp_server(tmp_path):
    """The worker stages ONE mcp.json for every harness, in the OpenHands spelling
    (`{"url": ...}`, no transport field). The Claude CLI's config is a discriminated
    union: an untyped entry with a url is read as a stdio server with no command and
    SKIPPED - the session opens with an empty tool list and the agent builds on
    without the browser, Context7, the KBs or the §Tools servers. Verified against
    claude-code 2.1.251, which answers the untyped form with `url_missing_type` and
    an empty `mcp_servers`, and registers the identical entry once type is present.
    """
    if not RUN_CLAUDE.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    mod = _run_claude_module()
    mod.OPENVISOR = tmp_path
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {
        "browser": {"url": "http://browser-mcp:8931/mcp"},
        "context7": {"url": "http://context7:8080/mcp", "headers": {"K": "v"}},
        "legacy": {"command": "npx", "args": ["-y", "some-mcp"]},
        "explicit": {"type": "sse", "url": "http://sidecar/sse"},
    }}))
    servers = mod._mcp_servers()
    assert servers["browser"] == {"url": "http://browser-mcp:8931/mcp", "type": "http"}
    assert servers["context7"]["type"] == "http"
    assert servers["context7"]["headers"] == {"K": "v"}   # the key still rides along
    assert servers["legacy"]["type"] == "stdio"
    assert servers["explicit"]["type"] == "sse"           # an explicit type is kept


def test_the_claude_driver_drops_a_server_it_cannot_type(tmp_path):
    """Neither url nor command is unusable in both dialects - drop it rather than
    hand the CLI an entry that makes it skip the whole config."""
    if not RUN_CLAUDE.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    mod = _run_claude_module()
    mod.OPENVISOR = tmp_path
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {
        "junk": {"headers": {"K": "v"}}, "ok": {"url": "http://x/mcp"}}}))
    assert list(mod._mcp_servers()) == ["ok"]


def test_the_claude_driver_reports_what_the_session_actually_got():
    """A dropped MCP server costs the agent its tools and nothing else - the build
    runs on. Without this line, a build that lost every tool reads exactly like one
    that had them, which is how the untyped config survived its first production
    run."""
    if not RUN_CLAUDE.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    mod = _run_claude_module()

    class _Feed:
        def __init__(self):
            self.events = []

        def append(self, ev):
            self.events.append(ev)

    feed = _Feed()
    mod._report_session(feed, type("Msg", (), {"data": {
        "mcp_servers": [{"name": "browser", "status": "connected"},
                        {"name": "context7", "status": "failed"}],
        "mcp_server_errors": [{"name": "github", "type": "url_missing_type",
                               "message": "Skipped"}],
    }})())
    assert len(feed.events) == 1
    assert feed.events[0]["kind"] == "error"
    assert "context7" in feed.events[0]["title"] and "github" in feed.events[0]["title"]
    assert "browser" not in feed.events[0]["title"]

    feed = _Feed()
    mod._report_session(feed, type("Msg", (), {"data": {
        "mcp_servers": [{"name": "browser", "status": "connected"}]}})())
    assert feed.events == []


def test_the_runner_image_pins_both_halves_of_the_claude_harness():
    """The SDK drives the `claude` CLI as a subprocess, so both are the harness.
    Unpinned, the fingerprint claims a configuration the image no longer has."""
    if not RUNNER_DOCKERFILE.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    df = RUNNER_DOCKERFILE.read_text()
    assert "@anthropic-ai/claude-code@${CLAUDE_CLI_VERSION}" in df
    assert "claude-agent-sdk==${CLAUDE_SDK_VERSION}" in df
    revision = dev_harness.HARNESSES["claude_sdk"].driver_revision
    for arg in ("CLAUDE_SDK_VERSION=", "CLAUDE_CLI_VERSION="):
        version = df.split(arg, 1)[1].split("\n", 1)[0].strip()
        assert version in revision, f"{arg}{version} missing from {revision}"


# ------------------------------------------------------------------- admin surface

@pytest.fixture(scope="module")
def client():
    # Module-scoped and context-managed so every request shares one loop; dispose on
    # the way in and out, the convention every HTTP module here follows.
    import asyncio

    from app.core.db import engine
    from app.services import events
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c
    asyncio.run(engine.dispose(close=False))


@pytest.fixture()
def admin_headers(client):
    app.dependency_overrides[require_admin] = lambda: None
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    try:
        yield {"X-CSRF-Token": tok}
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_admin_settings_expose_the_catalog(client, admin_headers):
    s = client.get("/api/admin/settings", headers=admin_headers).json()
    assert s["dev_harness_selection_enabled"] is False
    assert s["dev_harness_allowed"] == ["openhands"]
    assert s["dev_harness_default"] == "openhands"
    assert [h["id"] for h in s["dev_harnesses"]] == list(dev_harness.HARNESSES)


def test_admin_can_enable_and_narrow(client, admin_headers, second_harness):
    h = admin_headers
    r = client.put("/api/admin/settings", headers=h, json={
        "dev_harness_selection_enabled": True,
        "dev_harness_allowed": ["openhands", "probe"],
        "dev_harness_default": "probe",
    }).json()
    assert r["dev_harness_selection_enabled"] is True
    assert r["dev_harness_allowed"] == ["openhands", "probe"]
    assert r["dev_harness_default"] == "probe"


def test_unknown_harness_is_rejected(client, admin_headers):
    bad = client.put("/api/admin/settings", headers=admin_headers,
                     json={"dev_harness_allowed": ["openhands", "nope"]})
    assert bad.status_code == 422 and "nope" in bad.json()["detail"]


def test_a_default_outside_the_allowed_set_is_rejected(client, admin_headers, second_harness):
    bad = client.put("/api/admin/settings", headers=admin_headers,
                     json={"dev_harness_allowed": ["openhands"], "dev_harness_default": "probe"})
    assert bad.status_code == 422 and "probe" in bad.json()["detail"]


def test_project_pin_requires_the_feature_to_be_on(client, admin_headers, second_harness):
    with SyncSession() as db:
        p = _project(db)
        db.commit()
        pid = p.id

    # selection off: refuse to store a pin resolve() would ignore
    refused = client.patch(f"/api/admin/projects/{pid}", headers=admin_headers,
                           json={"dev_harness": "probe"})
    assert refused.status_code == 422 and "disabled" in refused.json()["detail"]

    client.put("/api/admin/settings", headers=admin_headers, json={
        "dev_harness_selection_enabled": True,
        "dev_harness_allowed": ["openhands", "probe"]})

    ok = client.patch(f"/api/admin/projects/{pid}", headers=admin_headers,
                      json={"dev_harness": "probe"})
    assert ok.status_code == 200 and ok.json()["dev_harness"] == "probe"

    # an id outside the allowed set is refused even with the feature on
    nope = client.patch(f"/api/admin/projects/{pid}", headers=admin_headers,
                        json={"dev_harness": "openhands-next"})
    assert nope.status_code == 422

    # null resets to inheriting the instance default
    cleared = client.patch(f"/api/admin/projects/{pid}", headers=admin_headers,
                           json={"dev_harness": None})
    assert cleared.status_code == 200 and cleared.json()["dev_harness"] is None


def test_a_pin_the_project_model_cannot_run_is_refused(client, admin_headers):
    """Refused HERE, where the admin can still act on it: the dispatcher degrades
    such a pin rather than failing the build, so storing it would leave a project
    pinned to a harness that never runs and a benchmark comparing a harness that
    never executed. The dev stack's OPENAI_MODEL is not a Claude model."""
    with SyncSession() as db:
        p = _project(db)
        db.commit()
        pid = p.id
    client.put("/api/admin/settings", headers=admin_headers, json={
        "dev_harness_selection_enabled": True,
        "dev_harness_allowed": ["openhands", "claude_sdk"]})

    refused = client.patch(f"/api/admin/projects/{pid}", headers=admin_headers,
                           json={"dev_harness": "claude_sdk"})
    assert refused.status_code == 422
    assert settings.openai_model in refused.json()["detail"]

    # ... and accepted once the project's endpoint serves a model it can drive.
    with SyncSession() as db:
        from app.core.encryption import encrypt
        from app.models import ModelEndpoint, ProjectModelConfig
        ep = ModelEndpoint(label="Anthropic test", provider="anthropic",
                           base_url="https://api.anthropic.com/v1",
                           api_key_enc=encrypt("sk-ant-test"), model_name="claude-sonnet-5")
        db.add(ep)
        db.flush()
        db.add(ProjectModelConfig(project_id=pid, endpoint_id=ep.id))
        db.commit()
        eid = ep.id
    try:
        ok = client.patch(f"/api/admin/projects/{pid}", headers=admin_headers,
                          json={"dev_harness": "claude_sdk"})
        assert ok.status_code == 200 and ok.json()["dev_harness"] == "claude_sdk"
    finally:
        with SyncSession() as db:
            db.execute(delete(ProjectModelConfig).where(
                ProjectModelConfig.project_id == pid))
            db.execute(delete(ModelEndpoint).where(ModelEndpoint.id == eid))
            db.commit()
