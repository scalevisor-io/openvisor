"""§Phase 1 #5: spec-derived acceptance checks (services/acceptance.py).

The security-critical property: an LLM-authored path or `contains` fragment can
NEVER carry a shell metacharacter into the deployer (which runs them in the demo
DinD). generate_checks validates strictly and drops anything unsafe; the whole
feature falls back to [] (boot-only) on any problem. Also: the results are
advisory and flow into the eval collector + report.
"""
from types import SimpleNamespace

from app.services import acceptance
from app.services.agent_eval.collect import to_run_record
from app.services.agent_eval.metrics import RunRecord
from app.services.agent_eval.report import aggregate


def test_valid_check_accepts_clean_check():
    c = acceptance._valid_check({"path": "/", "contains": ["Score", "canvas"], "desc": "home"})
    assert c == {"path": "/", "contains": ["Score", "canvas"], "desc": "home"}


def test_valid_check_rejects_shell_injection_in_path():
    for bad in ["/; rm -rf /", "/`id`", "/$(whoami)", "/x&&y", "/a|b", "/a b", "x/no-leading-slash",
                "/a\"b", "/a'b", "/a<b", "/a\nb"]:
        assert acceptance._valid_check({"path": bad, "contains": ["x"]}) is None, bad


def test_valid_check_drops_unsafe_fragments_and_requires_one():
    # a fragment with shell metachars is dropped; a check with none left is invalid
    assert acceptance._valid_check({"path": "/", "contains": ["`x`", "$y", "a\"b"]}) is None
    # a mix keeps only the clean fragment
    c = acceptance._valid_check({"path": "/", "contains": ["Score", "$(evil)"]})
    assert c["contains"] == ["Score"]


def test_valid_check_rejects_malformed():
    assert acceptance._valid_check("nope") is None
    assert acceptance._valid_check({"path": "/", "contains": "not-a-list"}) is None
    assert acceptance._valid_check({"path": "/", "contains": []}) is None
    assert acceptance._valid_check({"contains": ["x"]}) is None  # no path


def _stub_generate(monkeypatch, checks_json):
    monkeypatch.setattr(acceptance, "_project_context", lambda db, p: "spec text")
    monkeypatch.setattr(acceptance, "record_usage", lambda *a, **k: 0.0)
    monkeypatch.setattr(acceptance, "chat_json",
                        lambda *a, **k: (checks_json, {"model": "m", "input_tokens": 1, "output_tokens": 1}))


def test_generate_checks_validates_and_caps(monkeypatch):
    _stub_generate(monkeypatch, {"checks": [
        {"path": "/", "contains": ["Angry Birds", "canvas"], "desc": "home"},
        {"path": "/;evil", "contains": ["x"]},                 # injection -> dropped
        {"path": "/api/score", "contains": ["score"], "desc": "api"},
    ]})
    out = acceptance.generate_checks(None, SimpleNamespace(id="p", dev_request_id=None))
    assert [c["path"] for c in out] == ["/", "/api/score"]  # the injection check dropped


def test_generate_checks_falls_back_to_empty(monkeypatch):
    from app.agents.pipeline import LLMUnavailable
    monkeypatch.setattr(acceptance, "_project_context", lambda db, p: "spec")
    monkeypatch.setattr(acceptance, "record_usage", lambda *a, **k: 0.0)

    def boom(*a, **k):
        raise LLMUnavailable("down")
    monkeypatch.setattr(acceptance, "chat_json", boom)
    assert acceptance.generate_checks(None, SimpleNamespace(id="p", dev_request_id=None)) == []
    # a non-dict / non-list result also degrades to []
    _stub_generate(monkeypatch, {"nope": 1})
    assert acceptance.generate_checks(None, SimpleNamespace(id="p", dev_request_id=None)) == []


def _rec(**kw):
    base = dict(spec_id="s", speciality="general-webapp", harness_version="hv_x", model="m",
                attempt=1, final_state="deploying", boot_result=True, contract_ok=None,
                ci_status=None, security_blocking=0, security_ran=True, leak_blocked=False,
                leak_scanner_errored=False, input_tokens=1, output_tokens=0, credits=1.0,
                wall_clock_s=1.0)
    base.update(kw)
    return RunRecord(**base)


def test_report_acceptance_pass_rate():
    recs = [_rec(acceptance_passed=3, acceptance_total=4),
            _rec(acceptance_passed=1, acceptance_total=2),
            _rec(acceptance_passed=None, acceptance_total=None)]  # not-run excluded from denom
    rep = aggregate(recs)
    assert rep.acceptance_pass_rate == round(4 / 6, 4)  # (3+1)/(4+2)
