"""Agent-eval foundation (docs/HARNESS.md Phase 0): pure, offline unit tests -
harness-version fingerprinting, corpus loading/validation, deterministic run metrics,
failure triage, and report aggregation (esp. the cost + pass@1-vs-pass@k math)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.agent_eval import corpus, harness_version as hv, report, taxonomy
from app.services.agent_eval.metrics import RunRecord, gate_failed_open, gates_clear, is_pass


def _settings(**over):
    base = dict(dev_max_iterations_default=40, dev_run_timeout_minutes=20, dev_cmd_timeout_s=600,
                dev_boot_fix_attempts=1,
                ci_max_retries=2, security_fix_attempts=10, kb_retrieval_min_score=0.5,
                dev_boot_check=True, acceptance_checks_enabled=True,
                kb_rules_digest_max_chars=12_000, kb_procedures_k=3)
    base.update(over)
    return SimpleNamespace(**base)


def _rec(**over):
    base = dict(spec_id="s1", speciality="general-webapp", harness_version="hv_x", model="m",
                attempt=1, final_state="done", boot_result=True, contract_ok=True, ci_status="green",
                security_blocking=0, security_ran=True, leak_blocked=False, leak_scanner_errored=False,
                input_tokens=1000, output_tokens=200, credits=5.0, wall_clock_s=300.0, error="")
    base.update(over)
    return RunRecord(**base)


# ---- harness version ----
def test_harness_version_stable_and_order_independent(tmp_path):
    (tmp_path / "development_system.md").write_text("system rules")
    (tmp_path / "security_review.md").write_text("review rules")
    v1 = hv.compute_harness_version(_settings(), tmp_path)
    v2 = hv.compute_harness_version(_settings(), tmp_path)
    assert v1 == v2 and v1.startswith("hv_")


def test_harness_version_changes_with_caps_and_prompts(tmp_path):
    (tmp_path / "development_system.md").write_text("A")
    (tmp_path / "security_review.md").write_text("B")
    base = hv.compute_harness_version(_settings(), tmp_path)
    assert hv.compute_harness_version(_settings(dev_max_iterations_default=60), tmp_path) != base
    assert hv.compute_harness_version(_settings(dev_cmd_timeout_s=60), tmp_path) != base
    assert hv.compute_harness_version(_settings(kb_retrieval_min_score=0.7), tmp_path) != base
    assert hv.compute_harness_version(_settings(kb_rules_digest_max_chars=8_000), tmp_path) != base
    assert hv.compute_harness_version(_settings(kb_procedures_k=5), tmp_path) != base
    assert hv.compute_harness_version(_settings(), tmp_path, tool_preset_id="new+grep") != base
    (tmp_path / "development_system.md").write_text("A CHANGED")  # prompt edit shifts it
    assert hv.compute_harness_version(_settings(), tmp_path) != base


def test_harness_config_excludes_model():
    cfg = hv.harness_config(_settings())
    assert "model" not in str(cfg).lower().replace("dev_max", "")  # the model is tracked separately
    assert cfg["caps"]["dev_max_iterations"] == 40 and cfg["gates"]["boot_check"] is True
    assert cfg["schema"] == 3 and cfg["caps"]["dev_cmd_timeout_s"] == 600


# ---- corpus ----
def test_starter_corpus_loads_and_stratifies():
    specs = corpus.load_corpus()
    assert len(specs) >= 12
    strat = corpus.stratify(specs)
    assert {"general-webapp", "api-backend", "sovereign-eu", "data-ai"} <= set(strat)
    # the report-deliverable spec is present and flagged (exercises the boot-gate bug)
    audit = next(s for s in specs if s.deliverable_type == "audit_report")
    assert audit.from_scratch is False
    assert any(s.sovereign for s in specs)


def test_corpus_rejects_missing_field_and_duplicate_id(tmp_path):
    (tmp_path / "bad.json").write_text('{"id":"x","speciality":"general-webapp"}')  # no description
    with pytest.raises(ValueError, match="description"):
        corpus.load_corpus(tmp_path)
    (tmp_path / "bad.json").write_text('{"id":"dup","speciality":"g","description":"d"}')
    (tmp_path / "bad2.json").write_text('{"id":"dup","speciality":"g","description":"d"}')
    with pytest.raises(ValueError, match="duplicate"):
        corpus.load_corpus(tmp_path)


# ---- metrics ----
def test_pass_and_gates():
    assert is_pass(_rec()) is True
    assert gates_clear(_rec(final_state="failed")) is True and is_pass(_rec(final_state="failed")) is False
    assert is_pass(_rec(boot_result=False)) is False          # boot fail
    assert is_pass(_rec(boot_result=None)) is False           # fail-open does NOT count as clear
    assert is_pass(_rec(leak_blocked=True)) is False
    assert is_pass(_rec(security_blocking=1)) is False
    assert is_pass(_rec(security_ran=False)) is False         # review couldn't run
    assert is_pass(_rec(ci_status="failed")) is False
    assert is_pass(_rec(ci_status=None)) is True              # CI not applicable (GitHub path)
    assert is_pass(_rec(contract_ok=None)) is True            # unknown contract doesn't fail


def test_gate_failed_open():
    assert gate_failed_open(_rec(boot_result=None)) is True
    assert gate_failed_open(_rec(leak_scanner_errored=True)) is True
    assert gate_failed_open(_rec()) is False


# ---- taxonomy ----
def test_taxonomy_buckets():
    assert taxonomy.classify(_rec()) is None
    assert taxonomy.classify(_rec(final_state="failed", leak_blocked=True)) == "security-or-leak-block"
    assert taxonomy.classify(_rec(final_state="failed", security_blocking=2)) == "security-or-leak-block"
    assert taxonomy.classify(_rec(final_state="timeout")) == "ran-out-of-time"
    assert taxonomy.classify(_rec(final_state="failed", error="hit max_iterations")) == "ran-out-of-iterations"
    assert taxonomy.classify(_rec(final_state="failed", error="apply_patch hunk failed")) == "cascading-edit-failure"
    assert taxonomy.classify(_rec(final_state="failed", boot_result=True, contract_ok=False)) == "contract-violation"
    assert taxonomy.classify(_rec(final_state="failed", boot_result=False)) == "boot-failure"
    assert taxonomy.classify(_rec(final_state="failed", boot_result=None)) == "infra-error"
    # gates clean but not delivered -> the semantic residual
    assert taxonomy.classify(_rec(final_state="failed")) == "unresolved-needs-judge"
    assert all(taxonomy.classify(_rec(final_state="failed", boot_result=False)) in taxonomy.FAILURE_CATEGORIES
               for _ in range(1))


# ---- report ----
def test_report_pass_at_1_vs_pass_at_k_and_cost():
    recs = [
        _rec(spec_id="p", attempt=1, credits=4.0),                                   # pass first try
        _rec(spec_id="q", attempt=1, boot_result=False, final_state="failed", credits=6.0),  # fail 1
        _rec(spec_id="q", attempt=2, credits=3.0),                                   # pass on retry
        _rec(spec_id="r", attempt=1, boot_result=False, final_state="failed", credits=5.0),  # never passes
    ]
    rep = report.aggregate(recs)
    assert rep.n_specs == 3 and rep.n_attempts == 4
    assert rep.pass_at_1 == pytest.approx(1 / 3, abs=1e-4)   # only 'p' passed on attempt 1 (rate rounded 4dp)
    assert rep.pass_at_k == pytest.approx(2 / 3, abs=1e-4)   # 'p' and 'q' eventually
    assert rep.total_credits == pytest.approx(18.0)
    assert rep.credits_per_passing_build == pytest.approx(18.0 / 2)   # 2 passing attempts
    assert rep.credits_per_passing_spec == pytest.approx(18.0 / 2)    # 2 specs passed >=1
    assert taxonomy.FAILURE_CATEGORIES[1] in rep.failure_histogram    # boot-failure counted


def test_report_flags_mixed_harness_versions_and_gate_open():
    recs = [_rec(spec_id="a", harness_version="hv_1"),
            _rec(spec_id="b", harness_version="hv_2", boot_result=None, final_state="failed")]
    rep = report.aggregate(recs)
    assert any("harness version" in w for w in rep.warnings)
    assert rep.gate_failed_open_rate == pytest.approx(0.5)
    assert "Agent eval report" in report.render_markdown(rep)


def test_report_empty_is_safe():
    rep = report.aggregate([])
    assert rep.n_attempts == 0 and rep.credits_per_passing_build is None
    assert "no records" in rep.warnings
