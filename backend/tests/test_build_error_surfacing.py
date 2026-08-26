"""Build-error surfacing: the model preflight (park with the real reason before
spending a sandbox), the runner's structured error report driving the park copy,
and default-branch detection replacing the hardcoded 'main'."""
import json

import httpx

from app.workers import tasks


class _P:
    id = "p1"
    workspace_path = None
    ssh_private_key_enc = None
    dev_request_id = None


def _fake_models(monkeypatch, status=200, ids=None, exc=None,
                 probe_status=400, probe_exc=None, probe_text="invalid model"):
    def fake_get(url, **kw):
        if exc:
            raise exc

        class R:
            status_code = status

            @staticmethod
            def json():
                return {"data": [{"id": i} for i in (ids or [])]}
        return R()

    def fake_post(url, **kw):
        if probe_exc:
            raise probe_exc

        class R:
            status_code = probe_status
            text = probe_text
        return R()
    monkeypatch.setattr(tasks, "_project_model_config",
                        lambda db, project: ("http://llm.example", "k", "examplecloud/Qwen3.6-27B"))
    import httpx as _httpx
    monkeypatch.setattr(_httpx, "get", fake_get)
    monkeypatch.setattr(_httpx, "post", fake_post)


def test_preflight_parks_when_models_miss_and_probe_rejects(monkeypatch):
    monkeypatch.setattr(tasks.settings, "openhands_enabled", True)
    _fake_models(monkeypatch, ids=["all-team-models"], probe_status=400,
                 probe_text="Invalid model name passed in")
    err = tasks._model_preflight(None, _P())
    assert err and "examplecloud/Qwen3.6-27B" in err and "all-team-models" in err
    assert "Invalid model name" in err


def test_preflight_trusts_a_working_alias_over_models_list(monkeypatch):
    # Router gateways accept ids /models never lists (all-team-models vs
    # ds.chat.qwen36) - the 1-token probe is the ground truth.
    monkeypatch.setattr(tasks.settings, "openhands_enabled", True)
    _fake_models(monkeypatch, ids=["all-team-models"], probe_status=200)
    assert tasks._model_preflight(None, _P()) is None


def test_preflight_accepts_exact_tail_and_prefix_matches(monkeypatch):
    monkeypatch.setattr(tasks.settings, "openhands_enabled", True)
    for served in (["examplecloud/Qwen3.6-27B"], ["Qwen3.6-27B"], ["other/Qwen3.6-27B"]):
        _fake_models(monkeypatch, ids=served)
        assert tasks._model_preflight(None, _P()) is None, served


def test_preflight_fails_open(monkeypatch):
    monkeypatch.setattr(tasks.settings, "openhands_enabled", True)
    _fake_models(monkeypatch, exc=httpx.ConnectError("down"))
    assert tasks._model_preflight(None, _P()) is None
    _fake_models(monkeypatch, status=404)
    assert tasks._model_preflight(None, _P()) is None
    _fake_models(monkeypatch, ids=[])
    assert tasks._model_preflight(None, _P()) is None
    # the confirmation probe erroring is also inconclusive
    _fake_models(monkeypatch, ids=["something-else"], probe_exc=httpx.ConnectError("down"))
    assert tasks._model_preflight(None, _P()) is None
    monkeypatch.setattr(tasks.settings, "openhands_enabled", False)
    _fake_models(monkeypatch, ids=["something-else"])
    assert tasks._model_preflight(None, _P()) is None  # scaffold runs skip it


def test_runner_error_report_drives_the_park_copy(tmp_path, monkeypatch):
    p = _P()
    p.workspace_path = str(tmp_path)
    (tmp_path / ".openvisor").mkdir()
    (tmp_path / ".openvisor" / "error.json").write_text(json.dumps(
        {"category": "llm_model",
         "message": "the model endpoint rejected the configured model (400 invalid model name)"}))
    feed: list = []
    monkeypatch.setattr(tasks.devfeed, "append_event", lambda *a, **k: feed.append(a))
    chat, err = tasks._runner_exit_copy(p)
    assert "rejected the configured model" in chat and "Resume" in chat
    assert "invalid model name" in err
    assert feed and feed[0][1] == "error"
    # read-and-unlink: a second park doesn't reuse the stale report
    chat2, err2 = tasks._runner_exit_copy(p)
    assert err2 == "" and "exited with an error" in chat2


def test_runner_exit_copy_without_report_is_generic(tmp_path):
    p = _P()
    p.workspace_path = str(tmp_path)
    chat, err = tasks._runner_exit_copy(p)
    assert err == "" and "exited with an error" in chat


def test_resolve_base_branch_overrides_and_falls_back(monkeypatch):
    p = _P()
    p.ssh_private_key_enc = b"enc"
    monkeypatch.setattr(tasks, "decrypt", lambda v: "KEY")
    target = {"repo_id": "r1", "remote": "git@github.com:a/b.git", "base_branch": "main"}
    monkeypatch.setattr(tasks.repolib, "detect_default_branch", lambda uri, key: "master")
    tasks._resolve_base_branch(None, p, target)
    assert target["base_branch"] == "master"
    # detection failure keeps the fallback
    target = {"repo_id": "r1", "remote": "git@github.com:a/b.git", "base_branch": "main"}
    monkeypatch.setattr(tasks.repolib, "detect_default_branch", lambda uri, key: None)
    tasks._resolve_base_branch(None, p, target)
    assert target["base_branch"] == "main"
    # platform repo (no repo_id) never probes
    called: list = []
    monkeypatch.setattr(tasks.repolib, "detect_default_branch",
                        lambda uri, key: called.append(uri))
    tasks._resolve_base_branch(None, p, {"repo_id": None, "remote": "x", "base_branch": "main"})
    assert not called


def test_detect_default_branch_parses_symref(tmp_path, monkeypatch):
    from app.services import repos

    class Proc:
        returncode = 0
        stdout = "ref: refs/heads/master\tHEAD\ncad928bd\tHEAD\n"
        stderr = ""
    monkeypatch.setattr(repos.subprocess, "run", lambda *a, **k: Proc())
    assert repos.detect_default_branch("git@github.com:a/b.git", "KEY") == "master"
    Proc.returncode = 128
    assert repos.detect_default_branch("git@github.com:a/b.git", "KEY") is None
    # https remotes and missing keys never shell out
    assert repos.detect_default_branch("https://github.com/a/b.git", "KEY") is None
    assert repos.detect_default_branch("git@github.com:a/b.git", "") is None


def test_push_failure_hint_names_the_cause():
    assert "Allow write access" in tasks._push_failure_hint(
        "ERROR: Write access to repository not granted.\nfatal: Could not read")
    assert "protected" in tasks._push_failure_hint("remote: GitLab: protected branch")
    assert tasks._push_failure_hint("some other failure") == ""


# ---- §branch naming + working-repositories context (same dev-run surface) ----

def test_sanitize_branch():
    from app.services.naming import sanitize_branch
    assert sanitize_branch("feat/csv-export") == "feat/csv-export"
    assert sanitize_branch("f/#45-implement-issue") == "f/#45-implement-issue"
    assert sanitize_branch('  "Feat: CSV export!"  ') == "Feat-CSV-export"
    assert sanitize_branch("a//b///c") == "a/b/c"
    assert sanitize_branch("-lead/trail.-") == "lead/trail"
    assert sanitize_branch("main") is None
    assert sanitize_branch("bad..ref") is None
    assert sanitize_branch("x" * 100).__len__() <= 60
    assert sanitize_branch("") is None and sanitize_branch(None) is None


def test_project_branch_falls_back_to_legacy():
    p = _P()
    p.dev_branch = None
    assert tasks._project_branch(p) == tasks.AGENT_BRANCH
    p.dev_branch = "f/#45-implement-issue"
    assert tasks._project_branch(p) == "f/#45-implement-issue"


# ---- §working method plan gate ----

def test_plan_only_task_block_and_approved_plan_block(monkeypatch):
    class _Proj:
        id = "p1"
        name = "P"
        description = "d"
        speciality = "general-webapp"
        from_scratch = True
        sovereign = False
        sovereign_comment = None
        kind = "ai"
        kb_ids = None
        dev_request_id = None
        dev_plan = "old plan\nwith feedback"
        dev_plan_status = "proposed"
        org_id = "o1"
        use_global_memory = None

    monkeypatch.setattr(tasks, "_context_repos", lambda db, project: [])
    monkeypatch.setattr(tasks, "_effective_memory", lambda db, project: [])
    monkeypatch.setattr(tasks, "_project_files_meta", lambda db, project: [])
    monkeypatch.setattr(tasks.rag, "search", lambda *a, **k: [])
    from app.services import speciality as spec
    monkeypatch.setattr(spec, "deliverable_clause", lambda p: "x")
    monkeypatch.setattr(spec, "knowledge_tags", lambda p: [])
    monkeypatch.setattr(spec, "one_shot_example", lambda p: "")
    from app.agents import pipeline as pl
    monkeypatch.setattr(pl, "_project_context", lambda db, p: "ctx")

    text, _ = tasks._build_task_file(None, _Proj(), plan_only=True)
    assert "PLAN-ONLY RUN" in text and "plan.md" in text
    assert "Prior plan and customer feedback" in text and "old plan" in text

    text2, _ = tasks._build_task_file(None, _Proj(), approved_plan="THE PLAN")
    assert "Approved plan" in text2 and "THE PLAN" in text2
    assert "PLAN-ONLY" not in text2


def test_sandbox_docker_capability_block(monkeypatch):
    """§dev-docker: the task states the sandbox's docker capability either way -
    agents otherwise burn billed steps discovering that dockerd is absent (or
    never realize the stack IS locally bootable)."""
    class _Proj:
        id = "p1"
        name = "P"
        description = "d"
        speciality = "general-webapp"
        from_scratch = True
        sovereign = False
        sovereign_comment = None
        kind = "ai"
        kb_ids = None
        dev_request_id = None
        dev_plan = None
        dev_plan_status = None
        org_id = "o1"
        use_global_memory = None

    monkeypatch.setattr(tasks, "_context_repos", lambda db, project: [])
    monkeypatch.setattr(tasks, "_effective_memory", lambda db, project: [])
    monkeypatch.setattr(tasks, "_project_files_meta", lambda db, project: [])
    monkeypatch.setattr(tasks.rag, "search", lambda *a, **k: [])
    from app.services import speciality as spec
    monkeypatch.setattr(spec, "deliverable_clause", lambda p: "x")
    monkeypatch.setattr(spec, "knowledge_tags", lambda p: [])
    monkeypatch.setattr(spec, "one_shot_example", lambda p: "")
    from app.agents import pipeline as pl
    monkeypatch.setattr(pl, "_project_context", lambda db, p: "ctx")

    monkeypatch.setattr(tasks.settings, "dev_sandbox_docker", False)
    text, _ = tasks._build_task_file(None, _Proj())
    assert "Docker is NOT available inside this sandbox" in text
    monkeypatch.setattr(tasks.settings, "dev_sandbox_docker", True)
    text, _ = tasks._build_task_file(None, _Proj())
    assert "ARE available inside this sandbox" in text
    assert "NOT available" not in text


def test_read_plan(tmp_path):
    p = _P()
    p.workspace_path = str(tmp_path)
    assert tasks._read_plan(p) == ""
    (tmp_path / ".openvisor").mkdir()
    (tmp_path / ".openvisor" / "plan.md").write_text("  the plan \n")
    assert tasks._read_plan(p) == "the plan"


# ---- §auto_dev pull-request deliverable ----

def test_auto_dev_deliverable_is_pull_request():
    from app.services import speciality

    class _Auto:
        kind = "auto_dev"
        speciality = "general-webapp"

    class _Ai:
        kind = "ai"
        speciality = "general-webapp"

    assert speciality.deliverable_type(_Auto()) == "pull_request"
    clause = speciality.deliverable_clause(_Auto())
    assert "NOT a deployed demo" in clause and "compose.demo.yml" in clause
    assert speciality.one_shot_example(_Auto()) == ""
    # ai-kind unchanged
    assert speciality.deliverable_type(_Ai()) == "deployed_demo"
    assert speciality.one_shot_example(_Ai()) != ""


# ---- KB conventions reach the branch namer and the task ----

def test_branch_namer_includes_kb_conventions(monkeypatch):
    from app.agents import pipeline

    class _Hit:
        file = "local/README.md"
        path = "local/README.md#0"
        content = "For important edits, create a branch respecting: f/#- for features"

    from app.services import rag
    monkeypatch.setattr(rag, "search", lambda *a, **k: [_Hit()])
    captured: dict = {}

    def fake_chat_json(messages, **kw):
        captured["user"] = messages[1]["content"]
        return {"branch": "f/#45-implement"}, {"model": "m", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(pipeline, "chat_json", fake_chat_json)
    monkeypatch.setattr(pipeline, "record_usage", lambda *a, **k: 0)

    class _P:
        id = "p1"
        description = "policy"
        kb_ids = None

    out = pipeline.generate_branch_name(None, _P(), None)
    assert out == "f/#45-implement"
    assert "f/#- for features" in captured["user"]
    assert "AUTHORITATIVE" in captured["user"]


def test_branch_namer_survives_kb_outage(monkeypatch):
    from app.agents import pipeline
    from app.services import rag

    def boom(*a, **k):
        raise RuntimeError("meili down")

    monkeypatch.setattr(rag, "search", boom)
    monkeypatch.setattr(pipeline, "chat_json",
                        lambda *a, **k: ({"branch": "feat/x"}, {"model": "m", "input_tokens": 1,
                                                                "output_tokens": 1}))
    monkeypatch.setattr(pipeline, "record_usage", lambda *a, **k: 0)

    class _P:
        id = "p1"
        description = "policy"
        kb_ids = None

    assert pipeline.generate_branch_name(None, _P(), None) == "feat/x"


def test_open_pr_sends_owner_qualified_head(monkeypatch):
    from app.services import github

    monkeypatch.setattr(github, "find_open_pr", lambda *a, **k: None)
    sent: dict = {}

    class _Resp:
        status_code = 201

        @staticmethod
        def json():
            return {"number": 7, "html_url": "u"}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            sent.update(json)
            return _Resp()

    monkeypatch.setattr(github, "_client", lambda token=None: _Client())
    out = github.open_pr("acme", "storefront-v2", "feat/upgrade-tracelib-v4",
                         "master", title="t", body="b", token="x")
    assert out["number"] == 7
    assert sent["head"] == "acme:feat/upgrade-tracelib-v4"
    assert sent["base"] == "master"


# ---- §effort resolution for dev runs ----

def test_dev_effort_defaults_high_and_honors_endpoint(monkeypatch):
    from app.models import ModelEndpoint
    from app.models.models import ProjectModelConfig

    class _DB:
        def __init__(self, row=None, ep=None):
            self._row, self._ep = row, ep

        def query(self, model):
            class _Q:
                def __init__(self, row):
                    self._row = row

                def filter_by(self, **kw):
                    return self

                def first(self):
                    return self._row
            return _Q(self._row)

        def get(self, model, _id):
            return self._ep

    p = _P()
    assert tasks._project_reasoning_effort(_DB(), p) == "high"

    class _Row:
        endpoint_id = "e1"

    class _Ep:
        reasoning_effort = "medium"

    assert tasks._project_reasoning_effort(_DB(_Row(), _Ep()), p) == "medium"

    class _EpUnset:
        reasoning_effort = None

    assert tasks._project_reasoning_effort(_DB(_Row(), _EpUnset()), p) == "high"


def test_no_changes_park_names_the_iteration_cap(tmp_path, monkeypatch):
    """A session that died at its iteration cap must say so - the generic
    "no changes to publish" copy hid the real cause (prod, 2026-08-10)."""
    p = _P()
    p.workspace_path = str(tmp_path)
    (tmp_path / ".openvisor").mkdir()
    (tmp_path / ".openvisor" / "exit_reason.json").write_text(
        '{"reason": "max_iterations", "limit": 40}')
    msgs, errs = [], []
    monkeypatch.setattr(tasks, "_post_message",
                        lambda db, pid, thread, author, body: msgs.append(body))
    monkeypatch.setattr(tasks, "_dev_thread", lambda db, project: "main")
    monkeypatch.setattr(tasks, "_safe_transition", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_save_run",
                        lambda project, state, logs="", error="": errs.append(error))
    tasks._fail_no_changes(None, p, "logs")
    assert "40" in msgs[0] and "cap" in msgs[0].lower() and "Resume" in msgs[0]
    assert "iteration cap" in errs[0]
    # read-and-unlink: the marker never drives a later park's copy
    tasks._fail_no_changes(None, p, "logs")
    assert errs[1] == "The run produced no changes to publish"


def test_llm_unavailable_copy_names_budget_exhaustion():
    """finish_reason=length is a budget problem, not an outage - the fallback
    copy must not send customers checking the provider (prod regression)."""
    from app.services.llm import LLMUnavailable
    budget = tasks._llm_unavailable_copy(
        LLMUnavailable("empty completion (finish_reason=length) - the model likely "
                       "spent the entire token budget on reasoning"), "ask me that")
    assert "budget" in budget and "provider" not in budget
    outage = tasks._llm_unavailable_copy(LLMUnavailable("connect timeout"), "ask me that")
    assert "provider" in outage


def test_branch_namer_gets_standing_rules_digest(monkeypatch):
    """§KB tiers: the rules digests reach the branch namer DETERMINISTICALLY -
    similarity retrieval alone missed a KB-stated scheme in prod regression."""
    from app.agents import pipeline
    from app.services import rag

    monkeypatch.setattr(rag, "search", lambda *a, **k: [])
    monkeypatch.setattr(rag, "rules_digests", lambda db, kb_ids: [
        ("Team KB", "Branch naming: f/#<issue>- for features, b/#<issue>- for bugs")])
    captured: dict = {}

    def fake_chat_json(messages, **kw):
        captured["user"] = messages[1]["content"]
        return {"branch": "f/#67-bump"}, {"model": "m", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(pipeline, "chat_json", fake_chat_json)
    monkeypatch.setattr(pipeline, "record_usage", lambda *a, **k: 0)

    class _P:
        id = "p1"
        description = "policy"
        kb_ids = None

    assert pipeline.generate_branch_name(None, _P(), None) == "f/#67-bump"
    assert "Customer standing rules" in captured["user"]
    assert "f/#<issue>-" in captured["user"]


def test_preflight_parks_a_gateway_outage_without_a_sandbox(monkeypatch):
    """A 5xx from /models AND twice from the 1-token completion is an endpoint
    that is down (prod: a 502 page from the CDN in front of the model ended a
    build after its startup was billed). Park it as an outage - nothing to
    reconfigure, Resume later - before a sandbox is spent."""
    import time
    monkeypatch.setattr(tasks.settings, "openhands_enabled", True)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _fake_models(monkeypatch, status=502, probe_status=502, probe_text="<html>502 Bad Gateway")
    err = tasks._model_preflight(None, _P())
    assert err and tasks._MODEL_OUTAGE in err and "502" in err and "llm.example" in err


def test_preflight_fails_open_when_only_models_is_down(monkeypatch):
    import time
    monkeypatch.setattr(tasks.settings, "openhands_enabled", True)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _fake_models(monkeypatch, status=502, probe_status=200)
    assert tasks._model_preflight(None, _P()) is None
    _fake_models(monkeypatch, status=502, probe_status=400, probe_text="odd gateway")
    assert tasks._model_preflight(None, _P()) is None  # not this guard's call
