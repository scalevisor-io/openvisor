"""§Phase 2: per-speciality profile + deliverable-type prompt overlay (speciality.py).

A report track (audit_report / architecture_docs) is told to deliver its report as a
browsable static site served via the normal compose.demo.yml contract - so the boot
gate / contract lint / demo deploy pipeline delivers a REPORT unchanged instead of
parking it for lacking a demo. A deployed_demo track gets no overlay (the default
MVP-app instructions stand). The report tracks are exercised through a fixture
catalog: specialities.json is operator-editable data, so the machinery must work for
any catalog that declares such a track, not just the ids shipped today.
"""
from types import SimpleNamespace

import pytest

from app.services import speciality

_CATALOG = {
    "specialities": [
        {"id": "demo-track", "deliverable_type": "deployed_demo", "knowledge_tags": ["ocpa"]},
        {"id": "audit-track", "deliverable_type": "audit_report", "knowledge_tags": ["ocpa", "sbom"]},
        {"id": "docs-track", "deliverable_type": "architecture_docs"},
    ]
}


def _proj(spec):
    return SimpleNamespace(speciality=spec)


@pytest.fixture()
def catalog(monkeypatch):
    monkeypatch.setattr(speciality, "load_static", lambda _name: _CATALOG)


def test_deliverable_type_reads_shipped_specialities_json():
    # against the real shipped catalog: the default speciality is a deployed demo
    assert speciality.deliverable_type(_proj("general-webapp")) == "deployed_demo"
    assert speciality.deliverable_type(_proj(None)) == "deployed_demo"
    assert speciality.deliverable_type(_proj("does-not-exist")) == "deployed_demo"


def test_deliverable_type_per_track(catalog):
    assert speciality.deliverable_type(_proj("audit-track")) == "audit_report"
    assert speciality.deliverable_type(_proj("docs-track")) == "architecture_docs"


def test_is_report_track(catalog):
    assert speciality.is_report_track(_proj("audit-track")) is True
    assert speciality.is_report_track(_proj("docs-track")) is True
    assert speciality.is_report_track(_proj("demo-track")) is False


def test_deliverable_clause_overrides_for_report_tracks(catalog):
    audit = speciality.deliverable_clause(_proj("audit-track"))
    assert "REPORT" in audit and "SBOM" in audit
    # crucially still ships the demo contract so the existing pipeline delivers it
    assert "compose.demo.yml" in audit and "$PORT" in audit and "static" in audit.lower()

    arch = speciality.deliverable_clause(_proj("docs-track"))
    assert "ARCHITECTURE" in arch and "compose.demo.yml" in arch


def test_deliverable_clause_empty_for_deployed_demo(catalog):
    # a normal build gets no overlay - the placeholder renders to nothing
    assert speciality.deliverable_clause(_proj("demo-track")) == ""
    assert speciality.deliverable_clause(_proj(None)) == ""


def test_knowledge_tags_exposed(catalog):
    tags = speciality.knowledge_tags(_proj("audit-track"))
    assert "sbom" in tags and isinstance(tags, list)


def test_one_shot_scaffold_demo_shows_boot_contract():
    ex = speciality.one_shot_example(_proj("general-webapp"))
    # the worked example demonstrates the exact demo contract (the #1 failure class)
    assert "compose.demo.yml" in ex and "${PORT}:8080" in ex and "Dockerfile" in ex
    assert "COPY" in ex and "0.0.0.0:8080" in ex


def test_one_shot_scaffold_report_serves_static_site(catalog):
    ex = speciality.one_shot_example(_proj("audit-track"))
    assert "nginx" in ex and "report/" in ex and "${PORT}:80" in ex
    assert "application logic" in ex.lower()
