"""sidecars/email_tool.py: the MCP surface of the email tool.

The sidecars ship as their own image, so compose.dev mounts the source read-only
at /app/sidecars_src and these load it by file path, skipping where that mount
is absent (same discipline as test_pexels_sidecar / test_websearch_sidecar).

The sidecar decides nothing - the api does (test_email_tool.py). What is pinned
here is the relay itself:

  - `tools/list` answers WITHOUT auth and passes the tool-poisoning scan the
    worker runs before every build; a server failing either is dropped.
  - A call without the build's token is refused before any network call.
  - The token rides to the api's INTERNAL route as the Bearer, and the api's
    refusal comes back to the agent as its one-line reason, as a tool error.
  - The module is not called `email`: the sidecar directory is first on the
    import path, so that name would replace the stdlib package http.client
    needs.
"""
import importlib.util
import pathlib
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services import mcp_scan

SIDECARS_SRC = pathlib.Path("/app/sidecars_src")


@pytest.fixture(scope="module")
def sidecars():
    if not (SIDECARS_SRC / "email_tool.py").exists():
        pytest.skip("sidecar source not mounted at /app/sidecars_src")
    sys.path.insert(0, str(SIDECARS_SRC))
    try:
        spec = importlib.util.spec_from_file_location(
            "sidecars_app_under_test", SIDECARS_SRC / "app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(SIDECARS_SRC))


@pytest.fixture
def mod(sidecars):
    return sys.modules["email_tool"]


@pytest.fixture
def client(sidecars):
    return TestClient(sidecars.app)


def _rpc(client, method, params=None, headers=None):
    return client.post("/email/mcp", headers=headers or {},
                       json={"jsonrpc": "2.0", "id": 1, "method": method,
                             "params": params or {}}).json()


def _call(client, name, args, token="et1.tok.sig"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return _rpc(client, "tools/call", {"name": name, "arguments": args}, headers)["result"]


class FakeApi:
    """Stands in for httpx.AsyncClient, answering like the api's internal route."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, headers, None))
        return self.responses.pop(0)

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, headers, json))
        return self.responses.pop(0)


def test_mounted_under_email_and_never_named_email(sidecars):
    assert "email" in sidecars.SIDECARS
    assert not (SIDECARS_SRC / "email.py").exists()


def test_tools_list_needs_no_auth_and_passes_the_poisoning_scan(client):
    out = _rpc(client, "tools/list")
    tools = out["result"]["tools"]
    assert {t["name"] for t in tools} == {"send_email", "list_recipients"}
    assert mcp_scan.scan_tool_defs(tools) == []


def test_initialize_handshake(client):
    out = _rpc(client, "initialize")
    assert out["result"]["serverInfo"]["name"] == "email"


def test_no_token_is_refused_without_a_network_call(client, mod, monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("must not call the api without a token")

    monkeypatch.setattr(mod.httpx, "AsyncClient", explode)
    res = _call(client, "send_email", {"to": ["a@example.com"], "subject": "s", "body": "b"},
                token=None)
    assert res["isError"] is True and "not set up" in res["content"][0]["text"]


def test_send_relays_to_the_internal_route_with_the_token(client, mod, monkeypatch):
    api = FakeApi([httpx.Response(200, json={"ok": True, "sent_to": ["a@example.com"]})])
    monkeypatch.setattr(mod.httpx, "AsyncClient", api)
    res = _call(client, "send_email",
                {"to": "a@example.com; b@example.com", "subject": "  Done \n now", "body": "Hi"},
                token="et1.abc.def")
    assert res["isError"] is False and res["content"][0]["text"] == "Sent to a@example.com."
    method, url, headers, body = api.calls[0]
    assert method == "POST" and url.endswith("/internal/email-tool/send")
    assert headers == {"Authorization": "Bearer et1.abc.def"}
    assert body == {"to": ["a@example.com", "b@example.com"], "subject": "Done now", "body": "Hi"}


def test_the_api_refusal_reaches_the_agent_as_its_reason(client, mod, monkeypatch):
    api = FakeApi([httpx.Response(403, json={
        "detail": "Not an approved recipient: x@y.z. Call list_recipients for the "
                  "addresses this project may email."})])
    monkeypatch.setattr(mod.httpx, "AsyncClient", api)
    res = _call(client, "send_email", {"to": ["x@y.z"], "subject": "s", "body": "b"})
    assert res["isError"] is True
    assert res["content"][0]["text"].startswith("Not an approved recipient: x@y.z")


def test_a_used_up_quota_says_so(client, mod, monkeypatch):
    monkeypatch.setattr(mod.httpx, "AsyncClient",
                        FakeApi([httpx.Response(429, json={"detail": "Too many requests"})]))
    res = _call(client, "send_email", {"to": ["a@example.com"], "subject": "s", "body": "b"})
    assert res["isError"] is True and "quota" in res["content"][0]["text"]


def test_list_recipients_renders_compactly(client, mod, monkeypatch):
    api = FakeApi([httpx.Response(200, json={"recipients": [
        {"address": "owner@example.com", "label": "project customer"},
        {"address": "ops@example.com", "label": "approved by the administrator"}]})])
    monkeypatch.setattr(mod.httpx, "AsyncClient", api)
    res = _call(client, "list_recipients", {})
    assert res["content"][0]["text"] == ("- owner@example.com (project customer)\n"
                                         "- ops@example.com (approved by the administrator)")
    assert api.calls[0][0] == "GET" and api.calls[0][1].endswith("/internal/email-tool/recipients")


@pytest.mark.parametrize("args,needle", [
    ({"to": [], "subject": "s", "body": "b"}, "at least one"),
    ({"to": 5, "subject": "s", "body": "b"}, "list of email addresses"),
    ({"to": ["a@example.com"], "subject": " ", "body": "b"}, "required"),
])
def test_malformed_calls_never_reach_the_api(client, mod, monkeypatch, args, needle):
    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeApi([]))
    res = _call(client, "send_email", args)
    assert res["isError"] is True and needle in res["content"][0]["text"]


def test_an_unreachable_api_is_a_tool_error(client, mod, monkeypatch):
    class Down(FakeApi):
        async def post(self, *a, **kw):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(mod.httpx, "AsyncClient", Down([]))
    res = _call(client, "send_email", {"to": ["a@example.com"], "subject": "s", "body": "b"})
    assert res["isError"] is True
