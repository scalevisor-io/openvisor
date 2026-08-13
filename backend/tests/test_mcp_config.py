"""_mcp_config(db) assembles the dev run's MCP server map from the enabled
KnowledgeBase rows (§KB/MR3). These tests pin the wiring: `browser` is always
injected (the browser tool, not a KB); an enabled `context7` row injects
Context7 from settings and a disabled one removes it; enabled `mcp` rows become
servers carrying their (decrypted) API key as an Authorization: Bearer header;
colliding slugs get unique keys; a keyless mcp KB is url-only; and with no
context7 row at all we fall back to Context7 from settings (pre-MR2 DB).

Hermetic: each test runs in a session it rolls back, having first cleared the
context7 + mcp rows so it controls the exact set - the shared dev DB's seeded
rows are left untouched.
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.core.config import settings
from app.core.encryption import decrypt, encrypt
from app.core.db import SyncSession
from app.models import KnowledgeBase
from app.workers.tasks import _mcp_config


@pytest.fixture(autouse=True)
def _clean_mcp_scan(monkeypatch):
    """Default: every audited MCP server is reachable + clean, so the wiring tests
    never touch the network. Individual tests override to exercise poison/transient."""
    from app.services import mcp_scan
    monkeypatch.setattr(mcp_scan, "audit_server", lambda url, api_key=None: ([], None))


@pytest.fixture
def db():
    with SyncSession() as s:
        try:
            yield s
        finally:
            s.rollback()


def _reset_kbs(db):
    """Clear the context7 + mcp + websearch rows so the test owns the exact set
    (rolled back)."""
    db.execute(delete(KnowledgeBase).where(
        KnowledgeBase.kind.in_(("context7", "mcp", "websearch"))))
    db.flush()


def _add(db, **kw):
    kw.setdefault("name", "KB")
    kw.setdefault("enabled", True)
    kb = KnowledgeBase(**kw)
    db.add(kb)
    db.flush()
    return kb


def _servers(db, kb_ids=None):
    return json.loads(_mcp_config(db, kb_ids)[0])["mcpServers"]


def _secrets(db, kb_ids=None):
    return _mcp_config(db, kb_ids)[1]


def test_only_context7_enabled(db):
    _reset_kbs(db)
    _add(db, kind="context7", name="Context7", enabled=True)
    servers = _servers(db)
    assert "browser" in servers
    assert "context7" in servers
    assert servers["context7"]["url"] == settings.context7_mcp_url
    # No mcp KBs → only browser + context7.
    assert set(servers) == {"browser", "context7"}


def test_context7_disabled_removes_it(db):
    _reset_kbs(db)
    _add(db, kind="context7", name="Context7", enabled=False)
    servers = _servers(db)
    assert "context7" not in servers
    assert set(servers) == {"browser"}


def test_no_context7_row_falls_back_to_settings(db):
    # Pre-MR2 DB: no context7 row at all → inject Context7 from settings.
    _reset_kbs(db)
    servers = _servers(db)
    assert "browser" in servers
    assert "context7" in servers
    assert servers["context7"]["url"] == settings.context7_mcp_url


def test_mcp_kb_with_key_bearer_header_decrypted(db):
    _reset_kbs(db)
    plaintext = "notion-secret-key-abc123"
    kb = _add(db, kind="mcp", name="My Notion Docs",
              uri="https://notion.example/mcp",
              api_key_enc=encrypt(plaintext), enabled=True)
    servers = _servers(db)
    assert "browser" in servers
    assert "my_notion_docs" in servers
    entry = servers["my_notion_docs"]
    assert entry["url"] == "https://notion.example/mcp"
    # The header carries the real plaintext, not the ciphertext.
    assert entry["headers"] == {"Authorization": f"Bearer {plaintext}"}
    assert plaintext not in kb.api_key_enc  # stored value is ciphertext
    assert decrypt(kb.api_key_enc) == plaintext  # round-trip
    # The decrypted key is also returned for the leak scan's refuse-set.
    assert plaintext in _secrets(db)


def test_mcp_kb_without_key_is_url_only(db):
    _reset_kbs(db)
    _add(db, kind="mcp", name="Public Docs",
         uri="https://docs.example/mcp", api_key_enc=None, enabled=True)
    servers = _servers(db)
    entry = servers["public_docs"]
    assert entry == {"url": "https://docs.example/mcp"}
    assert "headers" not in entry


def test_two_mcp_kbs_unique_names(db):
    _reset_kbs(db)
    # Identical display names must not overwrite each other's server entry.
    _add(db, kind="mcp", name="Notes", uri="https://a.example/mcp", enabled=True)
    _add(db, kind="mcp", name="Notes", uri="https://b.example/mcp", enabled=True)
    servers = _servers(db)
    assert {"notes", "notes_2"} <= set(servers)
    urls = {servers["notes"]["url"], servers["notes_2"]["url"]}
    assert urls == {"https://a.example/mcp", "https://b.example/mcp"}


def test_disabled_mcp_kb_excluded(db):
    _reset_kbs(db)
    _add(db, kind="mcp", name="Off", uri="https://off.example/mcp", enabled=False)
    servers = _servers(db)
    assert "off" not in servers


def test_poisoned_mcp_server_dropped_from_build(db, monkeypatch):
    # A reachable server whose tool metadata trips the poisoning scan is excluded,
    # and its key never enters the leak-scan refuse-set (the server is gone).
    from app.services import mcp_scan
    _reset_kbs(db)
    _add(db, kind="mcp", name="Evil", uri="https://evil.example/mcp",
         api_key_enc=encrypt("evil-key-xyz"), enabled=True)
    monkeypatch.setattr(mcp_scan, "audit_server",
                        lambda url, api_key=None: (["evil-tool: suspicious phrase 'ignore all'"], None))
    monkeypatch.setattr("app.services.emailer.send_email", lambda *a, **k: True)
    assert "evil" not in _servers(db)
    assert "evil-key-xyz" not in _secrets(db)


def test_unreachable_mcp_server_kept_as_transient(db, monkeypatch):
    # A fetch error (server down) must NOT drop the server - transient outage != poison.
    from app.services import mcp_scan
    _reset_kbs(db)
    _add(db, kind="mcp", name="Down", uri="https://down.example/mcp", enabled=True)
    monkeypatch.setattr(mcp_scan, "audit_server",
                        lambda url, api_key=None: ([], "connect timeout"))
    assert "down" in _servers(db)


def test_websearch_kb_resolves_to_sidecar_with_bearer(db):
    _reset_kbs(db)
    _add(db, kind="websearch", name="Web search - Serper (Google)", uri="serper",
         api_key_enc=encrypt("serper-key-123"), enabled=True)
    servers = _servers(db)
    entry = servers["websearch_serper"]
    assert entry["url"] == f"{settings.websearch_mcp_url.rstrip('/')}/serper/mcp"
    assert entry["headers"] == {"Authorization": "Bearer serper-key-123"}
    # The provider key joins the leak-scan refuse-set like any KB key.
    assert "serper-key-123" in _secrets(db)


def test_websearch_kb_without_key_excluded(db):
    # Belt-and-braces: the API keeps keyless websearch rows disabled, but even an
    # enabled keyless row must not produce a server (it could never search).
    _reset_kbs(db)
    _add(db, kind="websearch", name="Serper", uri="serper",
         api_key_enc=None, enabled=True)
    assert not [s for s in _servers(db) if s.startswith("websearch")]


def test_websearch_kb_disabled_or_deselected_excluded(db):
    _reset_kbs(db)
    off = _add(db, kind="websearch", name="Serper", uri="serper",
               api_key_enc=encrypt("k"), enabled=False)
    assert not [s for s in _servers(db) if s.startswith("websearch")]
    off.enabled = True
    db.flush()
    other = _add(db, kind="mcp", name="Docs", uri="https://docs.example/mcp", enabled=True)
    assert "websearch_serper" in _servers(db, [off.id])
    assert not [s for s in _servers(db, [other.id]) if s.startswith("websearch")]


def test_browser_always_present(db):
    _reset_kbs(db)
    assert "browser" in _servers(db)
    _add(db, kind="context7", name="Context7", enabled=False)
    assert "browser" in _servers(db)


def test_secret_values_all_mcp_keys(db):
    _reset_kbs(db)
    _add(db, kind="mcp", name="A", uri="https://a.example/mcp",
         api_key_enc=encrypt("key-one-aaaa"), enabled=True)
    _add(db, kind="mcp", name="B", uri="https://b.example/mcp",
         api_key_enc=encrypt("key-two-bbbb"), enabled=True)
    _add(db, kind="mcp", name="NoKey", uri="https://c.example/mcp",
         api_key_enc=None, enabled=True)
    secrets = _secrets(db)
    assert "key-one-aaaa" in secrets
    assert "key-two-bbbb" in secrets
    # A keyless KB contributes nothing to the refuse-set.
    assert len([s for s in secrets if s.startswith("key-")]) == 2


def test_secret_values_empty_when_no_keys(db):
    _reset_kbs(db)
    _add(db, kind="mcp", name="NoKey", uri="https://c.example/mcp",
         api_key_enc=None, enabled=True)
    # No context7 API key configured in the test env, no mcp keys → nothing to refuse.
    assert _secrets(db) == ([settings.context7_api_key] if settings.context7_api_key
                            else [])


def test_kb_ids_none_keeps_all(db):
    _reset_kbs(db)
    c7 = _add(db, kind="context7", name="Context7", enabled=True)
    kb = _add(db, kind="mcp", name="Docs", uri="https://docs.example/mcp", enabled=True)
    assert set(_servers(db, None)) == {"browser", "context7", "docs"}
    # Selecting both explicitly is equivalent.
    assert set(_servers(db, [c7.id, kb.id])) == {"browser", "context7", "docs"}


def test_kb_ids_empty_drops_every_kb(db):
    _reset_kbs(db)
    _add(db, kind="context7", name="Context7", enabled=True)
    _add(db, kind="mcp", name="Docs", uri="https://docs.example/mcp", enabled=True)
    # [] = none: the browser tool stays (not a KB), every KB server goes.
    assert set(_servers(db, [])) == {"browser"}


def test_kb_ids_selection_narrows_mcp_rows(db):
    _reset_kbs(db)
    a = _add(db, kind="mcp", name="A", uri="https://a.example/mcp", enabled=True)
    _add(db, kind="mcp", name="B", uri="https://b.example/mcp",
         api_key_enc=encrypt("b-key-123"), enabled=True)
    servers = _servers(db, [a.id])
    assert "a" in servers and "b" not in servers
    # The deselected KB's key never enters the leak-scan refuse-set.
    assert "b-key-123" not in _secrets(db, [a.id])


def test_kb_ids_gates_context7_row(db):
    _reset_kbs(db)
    c7 = _add(db, kind="context7", name="Context7", enabled=True)
    other = _add(db, kind="mcp", name="Docs", uri="https://docs.example/mcp", enabled=True)
    assert "context7" in _servers(db, [c7.id])
    assert "context7" not in _servers(db, [other.id])


def test_kb_ids_selection_disables_settings_fallback(db):
    # With no context7 row at all, the settings fallback applies only to
    # non-selecting projects: an explicit selection can't reference a missing row.
    _reset_kbs(db)
    assert "context7" in _servers(db, None)
    assert "context7" not in _servers(db, [])


def test_kb_ids_cannot_reenable_disabled_kb(db):
    _reset_kbs(db)
    off = _add(db, kind="mcp", name="Off", uri="https://off.example/mcp", enabled=False)
    assert "off" not in _servers(db, [off.id])


def test_prepare_runner_inputs_writes_extra_secrets(db, tmp_path, monkeypatch):
    """_prepare_runner_inputs persists the embedded KB keys to
    .openvisor/leak_extra_secrets.txt (0600) for the runner leak scan, and omits the
    file when the build embeds no keys."""
    from app.workers import tasks

    _reset_kbs(db)
    _add(db, kind="mcp", name="Notion", uri="https://notion.example/mcp",
         api_key_enc=encrypt("super-secret-kb-key"), enabled=True)

    # No secret Memory rows (unknown project/org ids), global memory off - isolates
    # this test to the mcp.json / leak-scan-input plumbing.
    project = SimpleNamespace(workspace_path=str(tmp_path), ssh_private_key_enc=None,
                              id="no-such-project", org_id="no-such-org",
                              use_global_memory=False, kb_ids=None)
    # Stub the task-file builder (it needs a full project); we only exercise the
    # leak-scan-input plumbing here.
    monkeypatch.setattr(tasks, "_build_task_file", lambda *a, **k: ("task", []))

    tasks._prepare_runner_inputs(db, project)
    extra = tmp_path / ".openvisor" / "leak_extra_secrets.txt"
    assert extra.exists()
    assert "super-secret-kb-key" in extra.read_text().splitlines()
    assert (extra.stat().st_mode & 0o777) == 0o600

    # Rebuild with no keyed KBs → the file must be removed (assuming no context7 key
    # in the test env; if one is configured it stays, which is also correct).
    _reset_kbs(db)
    tasks._prepare_runner_inputs(db, project)
    if not settings.context7_api_key:
        assert not extra.exists()
