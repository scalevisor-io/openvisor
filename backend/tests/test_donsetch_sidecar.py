"""donsetch-mcp sidecar (§web research): the capability gate and the stdio bridge.

The bridge is a standalone image (donsetch/bridge.py); compose.dev mounts its
source read-only at /app/donsetch_src, so it loads by file path and skips where
that mount is absent (same discipline as test_websearch_sidecar).

The engine itself is not in this image, so these tests run the bridge against a
FAKE `donsetch mcp` - a stdio JSON-RPC script standing in for the real binary.
That keeps the plumbing under test for real (subprocess, newline framing, id
matching, session reuse) while pinning the part that is ours to get right: a
capability the admin turned off must be invisible to `tools/list` AND refused by
`tools/call`, because a filtered list is a courtesy to the model, not a gate.
"""
import importlib.util
import json
import os
import pathlib
import stat

import pytest
from fastapi.testclient import TestClient

BRIDGE_SRC = pathlib.Path("/app/donsetch_src/bridge.py")

# Stands in for `donsetch mcp`: reads JSON-RPC lines on stdin, answers on stdout.
# Emits a non-JSON chatter line first, the way the real engine logs, so the
# reader's "skip what isn't a frame" path is exercised rather than assumed.
FAKE_ENGINE = '''#!/usr/bin/env python3
import json, sys
TOOLS = [{"name": "web_search", "description": "s", "inputSchema": {"type": "object"}},
         {"name": "web_fetch", "description": "f", "inputSchema": {"type": "object"}},
         {"name": "web_crawl", "description": "c", "inputSchema": {"type": "object"}}]
print("[ghost] starting up", flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        continue
    if method == "initialize":
        out = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
               "serverInfo": {"name": "donsetch-engine", "version": "3.2.4"}}
    elif method == "tools/list":
        out = {"tools": TOOLS}
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        out = {"content": [{"type": "text", "text": "called " + str(name)}], "isError": False}
    else:
        out = {}
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": out}), flush=True)
'''


@pytest.fixture(scope="module")
def fake_engine(tmp_path_factory):
    path = tmp_path_factory.mktemp("donsetch") / "fake-donsetch"
    path.write_text(FAKE_ENGINE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture(scope="module")
def bridge(fake_engine):
    if not BRIDGE_SRC.exists():
        pytest.skip("donsetch bridge source not mounted at /app/donsetch_src")
    os.environ["DONSETCH_BIN"] = fake_engine
    spec = importlib.util.spec_from_file_location("donsetch_bridge_under_test", BRIDGE_SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DONSETCH_BIN = fake_engine
    return module


@pytest.fixture
def client(bridge):
    with TestClient(bridge.app) as c:
        yield c


def _rpc(client, caps, method, params=None, headers=None, id_=1):
    return client.post(f"/{caps}/mcp", headers=headers or {},
                       json={"jsonrpc": "2.0", "id": id_, "method": method,
                             "params": params or {}})


def test_parse_caps(bridge):
    assert bridge.parse_caps("search+fetch") == ["search", "fetch"]
    assert bridge.parse_caps("all") == ["search", "fetch", "crawl"]
    # Order is canonical, not caller-supplied, so one set is always one URL.
    assert bridge.parse_caps("crawl+search") == ["search", "crawl"]
    assert bridge.parse_caps("nonsense") == []


def test_unknown_capability_set_is_404(client):
    assert _rpc(client, "nonsense", "initialize").status_code == 404


def test_handshake_reports_the_enabled_set(client):
    r = _rpc(client, "search+fetch", "initialize").json()
    info = r["result"]["serverInfo"]
    # The run addresses the bridge, not the engine behind it.
    assert info["name"] == "donsetch"
    assert info["capabilities_enabled"] == ["search", "fetch"]


def test_tools_list_hides_disabled_capabilities(client):
    r = _rpc(client, "search", "tools/list").json()
    assert [t["name"] for t in r["result"]["tools"]] == ["web_search"]

    r = _rpc(client, "all", "tools/list").json()
    assert [t["name"] for t in r["result"]["tools"]] == ["web_search", "web_fetch", "web_crawl"]


def test_tools_list_needs_no_session(client):
    # The tool-poisoning vet fetches tools/list once, unauthenticated and with no
    # session - it must not be handed a run's engine, and must still answer.
    r = _rpc(client, "search+crawl", "tools/list").json()
    assert [t["name"] for t in r["result"]["tools"]] == ["web_search", "web_crawl"]


def test_disabled_capability_is_refused_not_merely_hidden(client):
    r = _rpc(client, "search", "tools/call", {"name": "web_fetch", "arguments": {}}).json()
    assert r["result"]["isError"] is True
    assert "disabled" in r["result"]["content"][0]["text"]


def test_enabled_capability_reaches_the_engine(client):
    r = _rpc(client, "search+fetch", "tools/call",
             {"name": "web_fetch", "arguments": {"url": "https://example.com"}}).json()
    assert r["result"]["isError"] is False
    assert r["result"]["content"][0]["text"] == "called web_fetch"


def test_unknown_tool_is_refused(client):
    # A future engine release that adds a fourth tool must be reviewed into
    # TOOL_CAPS before any run can call it.
    r = _rpc(client, "all", "tools/call", {"name": "web_exfiltrate", "arguments": {}}).json()
    assert r["result"]["isError"] is True
    assert "unknown tool" in r["result"]["content"][0]["text"]


def test_session_is_issued_and_reused(client, bridge):
    init = _rpc(client, "all", "initialize")
    sid = init.headers.get("mcp-session-id")
    assert sid and sid in bridge.SESSIONS
    engine = bridge.SESSIONS[sid]
    r = client.post("/all/mcp", headers={"mcp-session-id": sid},
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "web_search", "arguments": {}}})
    assert r.json()["result"]["content"][0]["text"] == "called web_search"
    # Same engine process served both - that is what keeps a run's browsing
    # state its own, and what the reaper later tears down.
    assert bridge.SESSIONS[sid] is engine


def test_sessionless_call_does_not_touch_a_run_engine(client, bridge):
    init = _rpc(client, "all", "initialize")
    sid = init.headers["mcp-session-id"]
    before = bridge.SESSIONS[sid].proc.pid
    _rpc(client, "all", "tools/list")
    assert bridge.SESSIONS[sid].proc.pid == before
