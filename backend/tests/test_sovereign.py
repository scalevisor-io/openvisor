"""§Phase 2: the deterministic sovereign-technology gate (services/sovereign.py).

A sovereign project must not depend on US-hyperscaler tech; this scans the built
workspace and reports every non-sovereign dependency (AWS/GCP/Azure SDKs,
registries, managed endpoints). High-precision markers only - a legitimate EU
stack (incl. Google Fonts, generic words) must NOT be false-flagged. scan_workspace
returns (findings, complete); complete=False means the scan didn't fully verify and
must never be recorded as 'clean'.
"""
from app.services import sovereign


def _mk(tmp_path, files: dict):
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_detects_aws_gcp_azure(tmp_path):
    _mk(tmp_path, {
        "requirements.txt": "flask\nboto3==1.34.0\ngoogle-cloud-storage\n",
        "package.json": '{"dependencies": {"@azure/storage-blob": "^12.0.0"}}',
        "Dockerfile": "FROM 1234.dkr.ecr.eu-west-1.amazonaws.com/base:latest\n",
    })
    findings, complete = sovereign.scan_workspace(tmp_path)
    assert complete is True
    assert {f["category"] for f in findings} == {"AWS", "Google Cloud", "Microsoft Azure"}


def test_clean_eu_stack_passes(tmp_path):
    _mk(tmp_path, {
        "requirements.txt": "flask\npsycopg2\nminio\nredis\n",
        "compose.demo.yml": 'services:\n  web:\n    ports: ["${PORT}:8080"]\n',
        "Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n",
        "app.py": "import os, minio\nfrom postgres import connect\n",
    })
    assert sovereign.scan_workspace(tmp_path) == ([], True)


def test_google_fonts_and_maps_are_not_flagged(tmp_path):
    # fonts/maps.googleapis.com are in a huge fraction of legit EU frontends and are
    # NOT the Cloud SDK - flagging them would block good builds (the #1 review finding).
    _mk(tmp_path, {
        "index.tsx": "<link href='https://fonts.googleapis.com/css2?family=Inter'/>",
        "map.js": "loadScript('https://maps.googleapis.com/maps/api/js')",
    })
    assert sovereign.scan_workspace(tmp_path) == ([], True)


def test_cloud_googleapis_still_flagged(tmp_path):
    _mk(tmp_path, {"app.py": "url='https://storage.googleapis.com/my-bucket/x'"})
    findings, _ = sovereign.scan_workspace(tmp_path)
    assert len(findings) == 1 and findings[0]["category"] == "Google Cloud"


def test_source_import_is_caught(tmp_path):
    _mk(tmp_path, {"app.py": "import boto3\nclient = boto3.client('s3')\n"})
    findings, _ = sovereign.scan_workspace(tmp_path)
    assert len(findings) == 1 and findings[0]["category"] == "AWS" and findings[0]["file"] == "app.py"


def test_skips_vendor_and_git_and_binary_dirs(tmp_path):
    _mk(tmp_path, {
        "node_modules/aws-sdk/index.js": "require('@aws-sdk/client-s3')",
        ".git/config": "boto3 amazonaws.com",
        "app.py": "print('sovereign, minio only')",
    })
    assert sovereign.scan_workspace(tmp_path) == ([], True)


def test_no_false_positive_on_generic_words(tmp_path):
    _mk(tmp_path, {"app.py": "f = lambda x: x\ncolor = 'azure'\nsupport = True\n"})
    assert sovereign.scan_workspace(tmp_path) == ([], True)


def test_symlink_is_skipped(tmp_path):
    (tmp_path / "secret.dat").write_text("boto3")  # .dat is not a scanned extension
    (tmp_path / "link.py").symlink_to(tmp_path / "secret.dat")
    # link.py is a scanned extension but a SYMLINK - it must be skipped, not followed.
    assert sovereign.scan_workspace(tmp_path) == ([], True)


def test_fix_instruction_is_self_labeled(tmp_path):
    findings = [{"file": "requirements.txt", "category": "AWS", "marker": "boto3"}]
    msg = sovereign.fix_instruction(findings)
    # must start with the sentinel so the boot-fix loop treats it as a sovereign
    # failure (own message + park), not a boot failure
    assert msg.startswith(sovereign.SOVEREIGN_FIX_PREFIX)
    assert "requirements.txt" in msg and "boto3" in msg and "MinIO" in msg
