"""Multi-line secret Memory (PEM keys, JSON credentials) reaches the sandbox intact.

Secret Memory is handed to the runner as `.openvisor/secrets.env`, a file the
entrypoint SOURCES - so the escaping in that writer is what decides whether a
private key survives. Single-quoted shell strings may span newlines, and the
writer already escapes embedded quotes the `'\\''` way, so multi-line values
work; these pin it, because the failure mode is silent and awful: a PEM key
truncated at its first newline turns into a shell syntax error, or worse, a
value that looks present and is subtly wrong.

The UI is the other half - the Value field is a textarea, not a single-line
input - but the byte fidelity is decided here.
"""
import subprocess

import pytest

from app.core.db import SyncSession
from app.core.encryption import encrypt
from app.models import Organization, Project, ProjectMemory
from app.workers import tasks

PEM = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
       "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
       "AAAAMwAAAAtzc2gtZWQyNTUxOQAAACDq8Uu/it's got a quote\n"
       "-----END OPENSSH PRIVATE KEY-----")
JSON_CRED = '{\n  "type": "service_account",\n  "private_key": "-----BEGIN\\nKEY-----"\n}'


def _project_with_secret(db, value):
    org = Organization(name="Sec Org", credit_balance=10.0)
    db.add(org)
    db.flush()
    project = Project(org_id=org.id, name="P", description="d", kind="ai",
                      status="development")
    db.add(project)
    db.flush()
    db.add(ProjectMemory(project_id=project.id, key="DEPLOY_KEY", author="customer",
                         value_enc=encrypt(value), is_secret=True))
    db.flush()
    return project


def _written(tmp_path, value):
    """Reproduce the secrets.env line the runner prep writes for one secret."""
    name = tasks._env_name("DEPLOY_KEY")
    return f"{name}='" + value.replace("'", "'\\''") + "'\n"


@pytest.mark.parametrize("value", [PEM, JSON_CRED, "single-line-token"])
def test_a_secret_survives_being_sourced_byte_for_byte(tmp_path, value):
    """The real check: write the file the way the prep does, source it with a
    POSIX shell, and compare bytes."""
    env_file = tmp_path / "secrets.env"
    env_file.write_text(_written(tmp_path, value))
    out = subprocess.run(
        ["sh", "-c", f'set -a; . "{env_file}"; set +a; printf "%s" "$DEPLOY_KEY"'],
        capture_output=True, text=True, check=True)
    assert out.stdout == value


def test_the_writer_keeps_newlines_and_quotes(tmp_path):
    line = _written(tmp_path, PEM)
    assert line.count("\n") == PEM.count("\n") + 1      # value newlines + terminator
    assert "'\\''" in line                              # the embedded quote escaped


def test_memory_stores_a_multiline_secret_unchanged():
    """Envelope encryption must not normalise or trim the value."""
    from app.core.encryption import decrypt
    with SyncSession() as db:
        try:
            project = _project_with_secret(db, PEM)
            row = (db.query(ProjectMemory)
                   .filter_by(project_id=project.id, key="DEPLOY_KEY").one())
            assert decrypt(row.value_enc) == PEM
        finally:
            db.rollback()


def test_a_value_with_a_trailing_newline_is_preserved(tmp_path):
    """OpenSSH keys end with one, and dropping it breaks some parsers."""
    value = PEM + "\n"
    env_file = tmp_path / "secrets.env"
    env_file.write_text(_written(tmp_path, value))
    out = subprocess.run(
        ["sh", "-c", f'set -a; . "{env_file}"; set +a; printf "%s" "$DEPLOY_KEY"'],
        capture_output=True, text=True, check=True)
    assert out.stdout == value
