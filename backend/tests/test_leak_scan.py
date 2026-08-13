"""Runner pre-publish leak scan (runner/leak_scan.py): the connected MCP-KB /
Context7 API keys embedded in mcp.json must be refused if the agent copies one
into a staged deliverable file. The worker hands those raw key values to the scan
via .openvisor/leak_extra_secrets.txt; this test proves a staged file containing
such a value is BLOCKED and that the input file is cleaned up after the run.

The runner ships as a separate image with no test harness of its own, so
compose.dev mounts it read-only at /app/runner_src and this test loads it by file
path; it skips cleanly wherever that mount is absent (e.g. the built api image)."""
import importlib.util
import pathlib
import shutil
import subprocess

import pytest

LEAK_SCAN = pathlib.Path("/app/runner_src/leak_scan.py")


@pytest.fixture(scope="module")
def leak_scan():
    if not LEAK_SCAN.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    if shutil.which("git") is None:
        pytest.skip("git not available in this container")
    spec = importlib.util.spec_from_file_location("leak_scan_under_test", LEAK_SCAN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(ws, *args):
    subprocess.run(["git", "-C", str(ws), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def workspace(tmp_path, leak_scan, monkeypatch):
    """A throwaway git workspace with a .openvisor/ dir, wired into the scanner's
    module-level WORKSPACE/OPENVISOR paths."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    openvisor = ws / ".openvisor"
    openvisor.mkdir()
    # The worker writes both scan-input files on every dispatch (empty included);
    # a missing one means the agent deleted .openvisor/ mid-run - tested separately.
    (openvisor / "leak_secret_env.txt").write_text("")
    (openvisor / "leak_kb.json").write_text("[]")
    monkeypatch.setattr(leak_scan, "WORKSPACE", ws)
    monkeypatch.setattr(leak_scan, "OPENVISOR", openvisor)
    return ws, openvisor


def test_staged_file_with_kb_key_is_blocked(leak_scan, workspace):
    ws, openvisor = workspace
    key = "connected-kb-secret-key-xyz789"
    (openvisor / "leak_extra_secrets.txt").write_text(key + "\n")
    # The agent copied the KB credential into a deliverable file and staged it.
    (ws / "README.md").write_text(f"# App\n\nAuth: Bearer {key}\n")
    _git(ws, "add", "README.md")

    assert leak_scan.main() == 1  # fail closed: nothing gets published
    # The scan input is cleaned up regardless of outcome.
    assert not (openvisor / "leak_extra_secrets.txt").exists()


def test_staged_file_without_kb_key_passes(leak_scan, workspace):
    ws, openvisor = workspace
    (openvisor / "leak_extra_secrets.txt").write_text("connected-kb-secret-key-xyz789\n")
    (ws / "README.md").write_text("# App\n\nA normal readme, no secrets.\n")
    _git(ws, "add", "README.md")

    assert leak_scan.main() == 0
    assert not (openvisor / "leak_extra_secrets.txt").exists()  # still cleaned up


def test_secret_values_reads_extra_secrets(leak_scan, workspace):
    _ws, openvisor = workspace
    (openvisor / "leak_extra_secrets.txt").write_text(
        "connected-kb-secret-key-xyz789\nsecond-kb-key-abcdef\n")
    vals = leak_scan._secret_values()
    assert "connected-kb-secret-key-xyz789" in vals
    assert "second-kb-key-abcdef" in vals


def test_base64_and_hex_encoded_secret_is_blocked(leak_scan, workspace):
    import base64

    ws, openvisor = workspace
    secret = "connected-kb-secret-key-xyz789"
    (openvisor / "leak_extra_secrets.txt").write_text(secret + "\n")
    b64 = base64.b64encode(secret.encode()).decode()
    hexed = secret.encode().hex()
    # The agent tried to smuggle the key out base64- and hex-encoded.
    (ws / "config.js").write_text(f"const a = '{b64}';\nconst b = '{hexed}';\n")
    _git(ws, "add", "config.js")
    assert leak_scan.main() == 1


def test_secret_in_agent_commit_history_is_blocked(leak_scan, workspace, tmp_path):
    # The agent committed a secret, then removed it from the working tree - the
    # staged snapshot is clean, but the secret is still in the pushed history.
    ws, openvisor = workspace
    secret = "connected-kb-secret-key-xyz789"
    (openvisor / "leak_extra_secrets.txt").write_text(secret + "\n")

    (ws / "app.py").write_text("print('hi')\n")
    _git(ws, "add", "app.py")
    _git(ws, "commit", "-q", "-m", "base")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(ws, "remote", "add", "origin", str(bare))
    _git(ws, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(ws, "fetch", "-q", "origin")
    # new commit carrying the secret, then a follow-up that scrubs the working tree
    (ws / "notes.txt").write_text(f"token={secret}\n")
    _git(ws, "add", "notes.txt")
    _git(ws, "commit", "-q", "-m", "wip")
    (ws / "notes.txt").write_text("token=REDACTED\n")
    _git(ws, "add", "notes.txt")
    _git(ws, "commit", "-q", "-m", "clean up")

    assert leak_scan._staged_files() == []      # nothing staged - file scan sees nothing
    assert leak_scan.main() == 1                # ...but the history scan blocks it


def test_secret_in_commit_message_is_blocked(leak_scan, workspace, tmp_path):
    ws, openvisor = workspace
    secret = "connected-kb-secret-key-xyz789"
    (openvisor / "leak_extra_secrets.txt").write_text(secret + "\n")
    (ws / "app.py").write_text("print('hi')\n")
    _git(ws, "add", "app.py")
    _git(ws, "commit", "-q", "-m", "base")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(ws, "remote", "add", "origin", str(bare))
    _git(ws, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(ws, "fetch", "-q", "origin")
    (ws / "app.py").write_text("print('hi again')\n")
    _git(ws, "add", "app.py")
    _git(ws, "commit", "-q", "-m", f"exfil the token {secret} in this message")
    assert leak_scan.main() == 1


def test_internal_error_fails_open_by_default_closed_on_env(leak_scan, workspace, monkeypatch):
    def boom():
        raise RuntimeError("scanner bug")

    monkeypatch.setattr(leak_scan, "_scan", boom)
    monkeypatch.delenv("LEAK_SCAN_FAIL_CLOSED", raising=False)
    assert leak_scan.main() == 0            # default: a scanner bug must not brick builds
    monkeypatch.setenv("LEAK_SCAN_FAIL_CLOSED", "1")
    assert leak_scan.main() == 1            # opt-in: block the publish on a scanner error


def test_missing_secret_names_file_is_internal_error(leak_scan, workspace, monkeypatch):
    # The agent ran `git clean -fdx` (or rm -rf'd .openvisor/): the scan inputs are
    # gone. That must surface as an internal error under the fail-open/closed
    # policy - never a quiet scan with a reduced refuse-set.
    ws, openvisor = workspace
    (openvisor / "leak_secret_env.txt").unlink()
    (ws / "README.md").write_text("# fine\n")
    _git(ws, "add", "README.md")
    monkeypatch.delenv("LEAK_SCAN_FAIL_CLOSED", raising=False)
    assert leak_scan.main() == 0
    monkeypatch.setenv("LEAK_SCAN_FAIL_CLOSED", "1")
    assert leak_scan.main() == 1


def test_missing_kb_fingerprints_file_is_internal_error(leak_scan, workspace, monkeypatch):
    ws, openvisor = workspace
    (openvisor / "leak_kb.json").unlink()
    (ws / "README.md").write_text("# fine\n")
    _git(ws, "add", "README.md")
    monkeypatch.setenv("LEAK_SCAN_FAIL_CLOSED", "1")
    assert leak_scan.main() == 1
    # LEAK_SCAN_KB=0 disables the KB half entirely - the file is then not required.
    monkeypatch.setenv("LEAK_SCAN_KB", "0")
    monkeypatch.delenv("LEAK_SCAN_FAIL_CLOSED", raising=False)
    (openvisor / "leak_secret_env.txt").write_text("")
    assert leak_scan.main() == 0
