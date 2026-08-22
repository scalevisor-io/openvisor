"""Saved model endpoints (§model config): admin-gated CRUD, api_key encryption +
masking, base-URL validation, delete-blocked-while-in-use, the per-project
model-config (endpoint_id + model_name, price-table validation, clear), and the
worker's _project_model_config resolution through a saved endpoint.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.core.encryption import decrypt
from app.core.security import hash_password
from app.main import app
from app.models import (
    ModelEndpoint, Organization, Project, ProjectModelConfig, User,
)
from app.workers import tasks

PRICED = "mistral-large-latest"  # present in the price table example


@pytest.fixture(scope="module")
def client():
    import asyncio

    from app.core.db import engine
    from app.services import events
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    with TestClient(app) as c:
        yield c


def _user(role: str):
    email = f"me-{uuid.uuid4().hex[:8]}@example.com"
    pwd = "me-secret-123"
    with SyncSession() as db:
        org = Organization(name="ME Org")
        db.add(org)
        db.flush()
        db.add(User(org_id=org.id, email=email, password_hash=hash_password(pwd),
                    role=role, email_verified=True))
        db.commit()
    return email, pwd


def _auth(client, email, pwd):
    tok = client.get("/api/auth/csrf").json()["csrf_token"]
    r = client.post("/api/auth/login", json={"email": email, "password": pwd},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": tok}


@pytest.fixture(scope="module")
def admin(client):
    """One admin login for the whole module (the login limiter is per-IP)."""
    email, pwd = _user("admin")
    return _auth(client, email, pwd)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with SyncSession() as db:
        db.execute(delete(ProjectModelConfig))
        db.execute(delete(ModelEndpoint))
        db.commit()


def _project() -> str:
    with SyncSession() as db:
        org = Organization(name="ME Proj Org")
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="P", description="d", kind="ai", status="draft")
        db.add(p)
        db.commit()
        return p.id


def test_requires_admin(client):
    client.cookies.clear()
    assert client.get("/api/admin/model-endpoints").status_code in (401, 403)
    email, pwd = _user("customer")
    h = _auth(client, email, pwd)
    assert client.get("/api/admin/model-endpoints", headers=h).status_code == 403


def _mk(label="E", provider="openai", base_url="https://api.openai.com/v1",
        api_key="k", model_name=PRICED, **extra):
    return {"label": label, "provider": provider, "base_url": base_url,
            "api_key": api_key, "model_name": model_name, **extra}


def test_crud_flow(client, admin):
    # -- create: key envelope-encrypted at rest, never returned; the model rides on it
    r = client.post("/api/admin/model-endpoints", headers=admin,
                    json=_mk(label="OpenAI prod", api_key="sk-secret", model_name=PRICED))
    assert r.status_code == 200, r.text
    ep = r.json()
    assert ep["provider"] == "openai" and ep["has_api_key"] is True
    assert ep["model_name"] == PRICED and ep["model_priced"] is True
    assert ep["input_price"] is None and ep["output_price"] is None  # table wins
    assert "api_key" not in ep and "api_key_enc" not in ep
    with SyncSession() as db:
        row = db.get(ModelEndpoint, ep["id"])
        assert row.api_key_enc != "sk-secret" and decrypt(row.api_key_enc) == "sk-secret"

    # -- non-http(s) base URL is rejected
    assert client.post("/api/admin/model-endpoints", headers=admin,
                       json=_mk(base_url="notaurl")).status_code == 400

    # -- patch: blank api_key keeps the current one; a new one replaces it
    client.patch(f"/api/admin/model-endpoints/{ep['id']}", headers=admin, json={"label": "Renamed"})
    with SyncSession() as db:
        assert decrypt(db.get(ModelEndpoint, ep["id"]).api_key_enc) == "sk-secret"  # kept
    client.patch(f"/api/admin/model-endpoints/{ep['id']}", headers=admin, json={"api_key": "sk-new"})
    with SyncSession() as db:
        assert decrypt(db.get(ModelEndpoint, ep["id"]).api_key_enc) == "sk-new"  # replaced

    # -- delete
    assert client.delete(f"/api/admin/model-endpoints/{ep['id']}", headers=admin).status_code == 200
    assert client.delete(f"/api/admin/model-endpoints/{ep['id']}", headers=admin).status_code == 404


def test_unknown_model_requires_inline_pricing(client, admin):
    # a model not in the price table is refused unless input+output cost is supplied
    r = client.post("/api/admin/model-endpoints", headers=admin,
                    json=_mk(provider="custom", model_name="acme-model-xyz"))
    assert r.status_code == 400
    # -- with the prices it saves; model_priced is false and the custom price shows
    ep = client.post("/api/admin/model-endpoints", headers=admin,
                     json=_mk(provider="custom", model_name="acme-model-xyz",
                              input_price=2.0, output_price=10.0))
    assert ep.status_code == 200, ep.text
    ep = ep.json()
    assert ep["model_priced"] is False
    assert ep["input_price"] == 2.0 and ep["output_price"] == 10.0


def test_unknown_model_billed_via_endpoint_price(client, admin):
    from app.core.config import settings
    from app.services.llm import record_usage

    UNPRICED = "acme-billed-model"
    client.post("/api/admin/model-endpoints", headers=admin,
                json=_mk(provider="custom", base_url="https://api.acme.ai/v1",
                         model_name=UNPRICED, input_price=2.0, output_price=10.0))
    with SyncSession() as db:
        org = Organization(name="Bill Org")
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="P", description="d", kind="ai", status="draft")
        db.add(p)
        db.flush()
        # 1M input * $2 + 1M output * $10 = $12, times the markup - billed off the
        # endpoint's custom price even though the model isn't in the static table.
        credits = record_usage(
            db, p, {"model": UNPRICED, "input_tokens": 1_000_000, "output_tokens": 1_000_000}, "test")
        db.rollback()
    assert credits == pytest.approx(12.0 * settings.credit_markup)


def test_endpoint_cached_price_bills_cache_reads_discounted(client, admin):
    from app.core.config import settings
    from app.services.llm import record_usage

    # cached rate above the input rate is a typo, not a discount
    r = client.post("/api/admin/model-endpoints", headers=admin,
                    json=_mk(provider="custom", model_name="acme-cached-model",
                             input_price=1.25, output_price=4.25, cached_input_price=2.0))
    assert r.status_code == 400
    ep = client.post("/api/admin/model-endpoints", headers=admin,
                     json=_mk(provider="custom", model_name="acme-cached-model",
                              input_price=1.25, output_price=4.25,
                              cached_input_price=0.15)).json()
    assert ep["cached_input_price"] == 0.15
    with SyncSession() as db:
        org = Organization(name="Cache Org")
        db.add(org)
        db.flush()
        p = Project(org_id=org.id, name="P", description="d", kind="ai", status="draft")
        db.add(p)
        db.flush()
        # 1M input of which 800k cache reads: 0.2M*$1.25 + 0.8M*$0.15 = $0.37
        credits = record_usage(
            db, p, {"model": "acme-cached-model", "input_tokens": 1_000_000,
                    "output_tokens": 0, "cached_input_tokens": 800_000}, "test")
        db.rollback()
    assert credits == pytest.approx(0.37 * settings.credit_markup)


def test_delete_blocked_while_in_use(client, admin):
    pid = _project()
    ep = client.post("/api/admin/model-endpoints", headers=admin,
                     json=_mk(provider="mistral", base_url="https://api.mistral.ai/v1")).json()
    client.put(f"/api/admin/projects/{pid}/model-config", headers=admin, json={"endpoint_id": ep["id"]})
    r = client.delete(f"/api/admin/model-endpoints/{ep['id']}", headers=admin)
    assert r.status_code == 409  # a project still points at it


def test_set_get_and_clear_model_config(client, admin):
    pid = _project()
    ep = client.post("/api/admin/model-endpoints", headers=admin, json=_mk()).json()

    # -- set → the row references the endpoint (model + creds come from it)
    assert client.put(f"/api/admin/projects/{pid}/model-config", headers=admin,
                      json={"endpoint_id": ep["id"]}).status_code == 200
    assert client.get(f"/api/admin/projects/{pid}/model-config",
                      headers=admin).json() == {"endpoint_id": ep["id"]}

    # -- a bad endpoint id 404s
    assert client.put(f"/api/admin/projects/{pid}/model-config", headers=admin,
                      json={"endpoint_id": "nope"}).status_code == 404

    # -- clear → the override row is removed (falls back to the global default)
    assert client.put(f"/api/admin/projects/{pid}/model-config", headers=admin,
                      json={"endpoint_id": None}).status_code == 200
    assert client.get(f"/api/admin/projects/{pid}/model-config",
                      headers=admin).json() == {"endpoint_id": None}
    with SyncSession() as db:
        assert db.execute(select(ProjectModelConfig).where(
            ProjectModelConfig.project_id == pid)).scalar_one_or_none() is None


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient: records requests, replays canned responses."""
    calls: list = []
    respond = staticmethod(lambda method, url, headers, json: _FakeResponse())

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _FakeAsyncClient.calls.append(("GET", url, headers, None))
        return _FakeAsyncClient.respond("GET", url, headers, None)

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.calls.append(("POST", url, headers, json))
        return _FakeAsyncClient.respond("POST", url, headers, json)


@pytest.fixture
def fake_http(monkeypatch):
    from types import SimpleNamespace

    from app.api import model_endpoints as me
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.respond = staticmethod(lambda m, u, h, j: _FakeResponse())
    # patch the router's httpx reference only - never the shared httpx module
    monkeypatch.setattr(me, "httpx", SimpleNamespace(AsyncClient=_FakeAsyncClient))
    return _FakeAsyncClient


def test_priced_models_lists_table_aliases(client, admin):
    r = client.get("/api/admin/model-endpoints/priced-models", headers=admin)
    assert r.status_code == 200
    assert PRICED in r.json()["models"]


def test_model_catalog_filters_and_prices(client, admin, fake_http):
    fake_http.respond = staticmethod(lambda m, u, h, j: _FakeResponse(payload={
        "data": [{"id": PRICED}, {"id": "acme-chat"},
                 {"id": "text-embedding-3-small"}, {"id": "whisper-1"}]}))
    r = client.post("/api/admin/model-endpoints/models", headers=admin,
                    json={"provider": "openai", "base_url": "https://api.openai.com/v1",
                          "api_key": "sk-x"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["error"] is None
    # non-chat families dropped, priced flag from the static table
    assert out["models"] == [{"id": "acme-chat", "priced": False},
                             {"id": PRICED, "priced": True}]
    method, url, headers, _ = fake_http.calls[0]
    assert (method, url) == ("GET", "https://api.openai.com/v1/models")
    assert headers["Authorization"] == "Bearer sk-x"


def test_model_catalog_anthropic_auth_and_stored_key(client, admin, fake_http):
    ep = client.post("/api/admin/model-endpoints", headers=admin,
                     json=_mk(provider="anthropic", base_url="https://api.anthropic.com/v1",
                              api_key="sk-ant-stored")).json()
    fake_http.respond = staticmethod(
        lambda m, u, h, j: _FakeResponse(payload={"data": [{"id": "claude-x"}]}))
    # no api_key in the body → the endpoint's stored (encrypted) key is used
    r = client.post("/api/admin/model-endpoints/models", headers=admin,
                    json={"provider": "anthropic", "base_url": "https://api.anthropic.com/v1",
                          "endpoint_id": ep["id"]})
    assert r.status_code == 200
    headers = fake_http.calls[0][2]
    assert headers["x-api-key"] == "sk-ant-stored" and "Authorization" not in headers
    # neither a key nor an endpoint id → 400
    assert client.post("/api/admin/model-endpoints/models", headers=admin,
                       json={"provider": "openai", "base_url": "https://api.openai.com/v1"},
                       ).status_code == 400


def test_gateway_presets_save_and_authenticate_with_bearer(client, admin, fake_http):
    """§model config presets: the OpenAI-compatible gateways (OpenRouter and the
    in-region EURouter / CARouter). The schema's provider list is what the admin
    form's preset buttons post, so an id missing from it 422s the save; and only
    Anthropic takes x-api-key - a gateway keyed that way would authenticate against
    nothing. A gateway model id also carries its own prices (it is not in the
    static table), which is the normal unpriced-model path."""
    gateways = [("openrouter", "https://openrouter.ai/api/v1"),
                ("eurouter", "https://api.eurouter.ai/v1"),
                ("carouter", "https://carouter.ai/v1")]
    for provider, base in gateways:
        r = client.post("/api/admin/model-endpoints", headers=admin, json=_mk(
            label=provider, provider=provider, base_url=base, api_key=f"sk-{provider}",
            model_name="openai/gpt-5", input_price=1.0, output_price=2.0))
        assert r.status_code == 200, r.text
        ep = r.json()
        assert ep["provider"] == provider and ep["model_priced"] is False

        fake_http.calls.clear()
        fake_http.respond = staticmethod(
            lambda m, u, h, j: _FakeResponse(payload={"data": [{"id": "openai/gpt-5"}]}))
        assert client.post("/api/admin/model-endpoints/models", headers=admin,
                           json={"provider": provider, "base_url": base,
                                 "endpoint_id": ep["id"]}).status_code == 200
        method, url, headers, _ = fake_http.calls[0]
        assert (method, url) == ("GET", f"{base}/models")
        assert headers["Authorization"] == f"Bearer sk-{provider}"
        assert "x-api-key" not in headers

    assert client.post("/api/admin/model-endpoints", headers=admin,
                       json=_mk(provider="nosuchrouter")).status_code == 422


def test_model_catalog_soft_fails_and_redacts(client, admin, fake_http):
    fake_http.respond = staticmethod(
        lambda m, u, h, j: _FakeResponse(status_code=401, text="bad key sk-leak used"))
    r = client.post("/api/admin/model-endpoints/models", headers=admin,
                    json={"provider": "openai", "base_url": "https://api.openai.com/v1",
                          "api_key": "sk-leak"})
    assert r.status_code == 200  # soft failure: the form falls back to a custom model
    out = r.json()
    assert out["models"] == [] and "401" in out["error"]
    assert "sk-leak" not in out["error"]


def test_endpoint_preflight_probes_both_surfaces(client, admin, fake_http):
    ep = client.post("/api/admin/model-endpoints", headers=admin,
                     json=_mk(model_name="gpt-5.6-luna", input_price=1.0,
                              output_price=6.0)).json()
    fake_http.respond = staticmethod(lambda m, u, h, j: _FakeResponse(
        status_code=403 if u.endswith("/chat/completions") else 200,
        text="You have insufficient permissions for this operation."))
    r = client.post(f"/api/admin/model-endpoints/{ep['id']}/test", headers=admin)
    assert r.status_code == 200, r.text
    out = r.json()
    # gpt-5-family: both build surfaces probed; the chat-completions denial surfaces
    assert out["chat_completions"]["ok"] is False
    assert "insufficient permissions" in out["chat_completions"]["error"]
    assert out["responses"]["ok"] is True
    urls = [u for _, u, _, _ in fake_http.calls]
    assert urls == ["https://api.openai.com/v1/chat/completions",
                    "https://api.openai.com/v1/responses"]
    # non-gpt-5 model → no /responses probe
    fake_http.calls.clear()
    fake_http.respond = staticmethod(lambda m, u, h, j: _FakeResponse())
    ep2 = client.post("/api/admin/model-endpoints", headers=admin,
                      json=_mk(label="M", provider="mistral",
                               base_url="https://api.mistral.ai/v1")).json()
    out = client.post(f"/api/admin/model-endpoints/{ep2['id']}/test", headers=admin).json()
    assert out["responses"] is None and out["chat_completions"]["ok"] is True
    # §chat images: a healthy chat surface is followed by the vision probe - there
    # is no capability API, so sending an image IS the discovery.
    assert [u for _, u, _, _ in fake_http.calls] == [
        "https://api.mistral.ai/v1/chat/completions",
        "https://api.mistral.ai/v1/chat/completions"]
    assert fake_http.calls[0][3]["max_tokens"] == 16  # non-openai cap param
    parts = fake_http.calls[1][3]["messages"][0]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert out["vision"] == {"supported": True,
                             "detail": "accepted - image attachments are enabled",
                             "source": "probe"}


def test_worker_resolves_config_through_endpoint(client, admin):
    pid = _project()
    ep = client.post("/api/admin/model-endpoints", headers=admin,
                     json=_mk(api_key="sk-live", model_name=PRICED)).json()
    client.put(f"/api/admin/projects/{pid}/model-config", headers=admin, json={"endpoint_id": ep["id"]})
    with SyncSession() as db:
        base, key, model = tasks._project_model_config(db, db.get(Project, pid))
    assert base == "https://api.openai.com/v1"
    assert key == "sk-live"          # decrypted from the endpoint
    assert model == PRICED           # the model comes from the endpoint


def test_reasoning_effort_accepts_custom_values():
    from app.schemas.schemas import ModelEndpointIn, ModelEndpointPatchIn
    import pytest as _pytest
    from pydantic import ValidationError

    body = ModelEndpointIn(label="x", base_url="https://a", api_key="k",
                           model_name="m", reasoning_effort="xhigh")
    assert body.reasoning_effort == "xhigh"
    assert ModelEndpointPatchIn(reasoning_effort="my-tier_2").reasoning_effort == "my-tier_2"
    assert ModelEndpointPatchIn(reasoning_effort="").reasoning_effort == ""  # reset
    with _pytest.raises(ValidationError):
        ModelEndpointIn(label="x", base_url="https://a", api_key="k",
                        model_name="m", reasoning_effort="has spaces!")
    with _pytest.raises(ValidationError):
        ModelEndpointPatchIn(reasoning_effort="way-too-long-for-the-column")
