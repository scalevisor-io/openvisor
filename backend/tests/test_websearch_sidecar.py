"""websearch-mcp sidecar (§KB websearch kind): the MCP protocol surface and the
Serper provider formatting. The sidecar is a standalone image (mcp/websearch.py);
compose.dev mounts its source read-only at /app/mcp_src, so it loads by file path
and skips where that mount is absent (same discipline as test_mcp_scopes).

Pinned here: the initialize/tools-list handshake the worker's tool-poisoning vet
performs before every build, key handling (bearer forwarded per request, never
required for tools/list, tool error - not a protocol error - without one), and
that provider failures surface as agent-visible tool errors that carry neither
the key nor the query.
"""
import importlib.util
import pathlib

import pytest
from fastapi.testclient import TestClient

WEBSEARCH_SRC = pathlib.Path("/app/mcp_src/websearch.py")


@pytest.fixture(scope="module")
def ws():
    if not WEBSEARCH_SRC.exists():
        pytest.skip("websearch sidecar source not mounted at /app/mcp_src")
    spec = importlib.util.spec_from_file_location("websearch_under_test", WEBSEARCH_SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(ws):
    return TestClient(ws.app)


def _rpc(client, method, params=None, headers=None, id_=1):
    return client.post("/serper/mcp", headers=headers or {},
                       json={"jsonrpc": "2.0", "id": id_, "method": method,
                             "params": params or {}})


def test_handshake_and_tools_list_need_no_key(client):
    # The worker's poisoning vet fetches tools/list; it must work unauthenticated.
    init = _rpc(client, "initialize").json()
    assert init["result"]["serverInfo"]["name"] == "websearch-serper"
    assert "tools" in init["result"]["capabilities"]
    assert _rpc(client, "notifications/initialized").json()["result"] == {}
    tools = _rpc(client, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["web_search"]
    assert "query" in tools[0]["inputSchema"]["properties"]


def test_unknown_provider_404(client):
    r = client.post("/nope/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 404 and "unknown provider" in r.json()["error"]["message"]


def test_unknown_method_rpc_error(client):
    assert _rpc(client, "resources/list").json()["error"]["code"] == -32601


def test_call_without_key_is_tool_error(client):
    res = _rpc(client, "tools/call",
               {"name": "web_search", "arguments": {"query": "anything"}}).json()["result"]
    assert res["isError"] is True
    assert "no API key" in res["content"][0]["text"]


def test_call_forwards_bearer_and_clamps_num(client, ws, monkeypatch):
    seen = {}

    async def fake_search(key, query, num):
        seen.update(key=key, query=query, num=num)
        return "1. Result"

    monkeypatch.setitem(ws.PROVIDERS, "serper", fake_search)
    res = _rpc(client, "tools/call",
               {"name": "web_search", "arguments": {"query": "openhands sdk", "num_results": 99}},
               headers={"Authorization": "Bearer sk-serper"}).json()["result"]
    assert res == {"content": [{"type": "text", "text": "1. Result"}], "isError": False}
    assert seen == {"key": "sk-serper", "query": "openhands sdk", "num": ws.MAX_RESULTS}


def test_provider_error_hides_key_and_query(client, ws, monkeypatch):
    async def boom(key, query, num):
        raise ws.ProviderError("the Serper API key was rejected (ask the platform admin to re-check it)")

    monkeypatch.setitem(ws.PROVIDERS, "serper", boom)
    res = _rpc(client, "tools/call",
               {"name": "web_search", "arguments": {"query": "s3cret query"}},
               headers={"Authorization": "Bearer sk-serper"}).json()["result"]
    assert res["isError"] is True
    text = res["content"][0]["text"]
    assert "rejected" in text and "sk-serper" not in text and "s3cret" not in text


def test_empty_query_is_tool_error(client):
    res = _rpc(client, "tools/call",
               {"name": "web_search", "arguments": {"query": "  "}},
               headers={"Authorization": "Bearer k"}).json()["result"]
    assert res["isError"] is True


@pytest.mark.asyncio
async def test_serper_formatting(ws, monkeypatch):
    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "answerBox": {"answer": "42"},
                "organic": [
                    {"title": "OpenHands SDK", "link": "https://a.example/x",
                     "snippet": "  spaced   out  ", "date": "Jan 2, 2026"},
                    {"title": "Other", "link": "https://b.example/y", "snippet": "z"},
                ],
            }

    class FakeClient:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            assert kw["headers"]["X-API-KEY"] == "k"
            assert kw["json"] == {"q": "q", "num": 2}
            return FakeResp()

    monkeypatch.setattr(ws.httpx, "AsyncClient", FakeClient)
    text = await ws._serper_search("k", "q", 2)
    assert text.splitlines()[0] == "Answer box: 42"
    assert "1. OpenHands SDK (Jan 2, 2026)" in text
    assert "https://a.example/x" in text
    assert "spaced out" in text  # whitespace collapsed


@pytest.mark.asyncio
async def test_staan_formatting(ws, monkeypatch):
    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"web": {"results": [
                {"title": "Loi de finances", "url": "https://s.example/a",
                 "snippet": "un résumé", "published_date": "2026-01-02",
                 "extra_snippets": [{"chunk": "un extrait de page", "score": 0.9}]},
                {"title": "Autre", "url": "https://s.example/b", "snippet": "x"},
                {"title": "Trop", "url": "https://s.example/c", "snippet": "y"},
            ]}}

    class FakeClient:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **kw):
            assert url.endswith("/search/web")
            assert kw["headers"]["Authorization"] == "Bearer k"
            assert kw["params"]["q"] == "q" and "market" in kw["params"]
            return FakeResp()

    monkeypatch.setattr(ws.httpx, "AsyncClient", FakeClient)
    text = await ws._staan_search("k", "q", 2)
    assert "1. Loi de finances (2026-01-02)" in text
    assert "> un extrait de page" in text
    assert "Trop" not in text  # num caps the result slice


@pytest.mark.asyncio
async def test_staan_auth_failure_raises_provider_error(ws, monkeypatch):
    class FakeResp:
        status_code = 403

    class FakeClient:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, **kw): return FakeResp()

    monkeypatch.setattr(ws.httpx, "AsyncClient", FakeClient)
    with pytest.raises(ws.ProviderError):
        await ws._staan_search("bad", "q", 1)


@pytest.mark.asyncio
async def test_serper_auth_failure_raises_provider_error(ws, monkeypatch):
    class FakeResp:
        status_code = 403

    class FakeClient:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw): return FakeResp()

    monkeypatch.setattr(ws.httpx, "AsyncClient", FakeClient)
    with pytest.raises(ws.ProviderError):
        await ws._serper_search("bad", "q", 1)
