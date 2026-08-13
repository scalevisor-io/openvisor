"""§KB/§Tools: the name the admin pages show must be the name the run uses.

Whoever writes project instructions ("use the web_search tool") reads that
string off the admin page; if the dispatcher derived it differently the
instruction would name a tool that does not exist in the run.
"""
import json
from types import SimpleNamespace

from app.services import mcp_names


def _kb(kind, name="Some source", uri=None):
    return SimpleNamespace(kind=kind, name=name, uri=uri)


def test_names_per_kind():
    assert mcp_names.kb_server_name(_kb("websearch", "Web search - Staan (European index)",
                                        uri="staan")) == "websearch_staan"
    assert mcp_names.kb_server_name(_kb("websearch", "Web search - Serper (Google)",
                                        uri="serper")) == "websearch_serper"
    assert mcp_names.kb_server_name(_kb("context7", "Context7")) == "context7"
    assert mcp_names.kb_server_name(_kb("mcp", "Notion workspace")) == "notion_workspace"
    # retrieval sources are read into the task, never called
    assert mcp_names.kb_server_name(_kb("local", "Local knowledge")) is None
    assert mcp_names.kb_server_name(_kb("git", "Handbook repo")) is None
    assert mcp_names.kb_tools(_kb("websearch", uri="staan")) == ["web_search"]
    assert mcp_names.kb_tools(_kb("mcp")) == []
    assert mcp_names.tool_server_name(SimpleNamespace(slug="github")) == "github"


def test_collisions_get_a_suffix_only_within_one_run():
    used = {"browser", "context7"}
    first = mcp_names.server_name("Docs", used)
    used.add(first)
    assert first == "docs"
    assert mcp_names.server_name("docs!", used) == "docs_2"
    # the admin page shows the base form (no run context to collide within)
    assert mcp_names.server_name("Docs") == "docs"


def test_dispatcher_and_admin_payload_agree(monkeypatch):
    """The one that matters: build a run's mcp.json and check the websearch
    server key equals what the KB payload advertises."""
    from app.core.db import SyncSession
    from app.models import KnowledgeBase
    from app.workers import tasks

    with SyncSession() as db:
        kb = db.query(KnowledgeBase).filter(KnowledgeBase.kind == "websearch",
                                            KnowledgeBase.uri == "staan").first()
        assert kb is not None, "seed creates the staan row"
        advertised = mcp_names.kb_server_name(kb)

        kb.enabled, kb.api_key_enc = True, __import__(
            "app.core.encryption", fromlist=["encrypt"]).encrypt("k")
        monkeypatch.setattr(tasks, "_vet_mcp_server", lambda *a, **kw: True)
        cfg, _ = tasks._mcp_config(db, kb_ids=[kb.id])
        db.rollback()

    assert advertised in json.loads(cfg)["mcpServers"], (
        f"the run names it something else: {list(json.loads(cfg)['mcpServers'])}")
