"""§Phase 2: DevSecOps SBOM/CVE gate (services/sbom.py + speciality.is_devsecops).

A devsecops-hardened build is BLOCKED on a CRITICAL known-CVE dependency (the deployer
generates the SBOM + trivy-scans it; this decides pass/fail and the scoped fix). HIGH is
advisory. The gate fails OPEN on any scanner error (scanned=False -> no blocking), and its
fix message is self-labeled so the run loop parks it as a security-scan failure, never a
boot failure.
"""
from types import SimpleNamespace

from app.services import sbom, speciality


def test_is_devsecops_predicate():
    assert speciality.is_devsecops(SimpleNamespace(speciality="devsecops-hardened")) is True
    assert speciality.is_devsecops(SimpleNamespace(speciality="general-webapp")) is False
    assert speciality.is_devsecops(SimpleNamespace(speciality=None)) is False


def test_blocking_only_criticals():
    scan = {"scanned": True, "findings": [
        {"id": "CVE-2021-1", "severity": "CRITICAL", "pkg": "lodash", "version": "4.17.15", "fixed": "4.17.21"},
        {"id": "CVE-2021-2", "severity": "HIGH", "pkg": "axios", "version": "0.21.1", "fixed": "0.21.2"},
    ]}
    blk = sbom.blocking(scan)
    assert len(blk) == 1 and blk[0]["id"] == "CVE-2021-1"  # HIGH is advisory


def test_blocking_fails_open_on_unscanned():
    assert sbom.blocking({"scanned": False, "error": "trivy pull failed"}) == []
    assert sbom.blocking(None) == []
    assert sbom.blocking({"scanned": True, "findings": []}) == []


def test_fix_instruction_is_self_labeled_and_actionable():
    scan = {"scanned": True, "findings": [
        {"id": "CVE-2021-23337", "severity": "CRITICAL", "pkg": "lodash", "version": "4.17.15", "fixed": "4.17.21"}]}
    msg = sbom.fix_instruction(scan)
    assert msg.startswith(sbom.SBOM_FIX_PREFIX)  # so the run loop labels it a security-scan failure
    assert "lodash" in msg and "CVE-2021-23337" in msg and "4.17.21" in msg


def test_fix_instruction_handles_no_fixed_version():
    scan = {"scanned": True, "findings": [
        {"id": "CVE-X", "severity": "CRITICAL", "pkg": "abandoned", "version": "1.0", "fixed": None}]}
    msg = sbom.fix_instruction(scan)
    assert "no fixed version" in msg and "abandoned" in msg


def test_special_gate_distinguishes_sentinels_and_survives_unwrapped():
    from app.services import sovereign
    from app.workers import tasks
    # each gate's message is labeled with its OWN run-error, never mislabeled a boot failure
    assert tasks._special_gate(sbom.SBOM_FIX_PREFIX + " …")[0] == "Security scan failed"
    assert tasks._special_gate(sovereign.SOVEREIGN_FIX_PREFIX + " …")[0] == "Sovereign gate failed"
    assert tasks._special_gate("The project FAILED the demo boot check …") is None
    # the SBOM fix message survives _boot_fix_instruction unwrapped (not boot-framed)
    msg = sbom.fix_instruction({"scanned": True, "findings": [
        {"id": "C", "severity": "CRITICAL", "pkg": "p", "version": "1", "fixed": "2"}]})
    assert tasks._boot_fix_instruction(msg) == msg
