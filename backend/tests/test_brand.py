"""White-label brand rendering: helper units, prompt placeholder coverage, and
the public /api/settings payload."""
from fastapi.testclient import TestClient

from app.agents.pipeline import PROMPT_DIR, load_prompt
from app.core.config import settings
from app.main import app
from app.services import brand
from app.services.knowledge import _load_prompt
from app.services.pricing import load_static


def test_subject_prefixes_brand():
    assert brand.subject("Hello") == f"[{settings.brand_name}] Hello"


def test_render_substitutes_all_placeholders():
    text = ("{{BRAND_NAME}} run by {{CONSULTANT_NAME}} ({{CONSULTANT_FIRST_NAME}}, "
            "{{CONSULTANT_FOCUS}}) on *.{{DEPLOY_DOMAIN}}")
    out = brand.render(text)
    assert "{{" not in out
    assert settings.brand_name in out
    # §consultant identity: the placeholders render the RESOLVED name (the
    # admin's stored pair, else CONSULTANT_NAME) - asserting the env value here
    # would pass only on an instance whose admin never set one.
    assert brand.consultant_name() in out
    assert brand.consultant_first_name() in out
    assert settings.consultant_focus in out
    assert settings.deploy_domain in out


def test_render_obj_recurses():
    data = {"a": ["{{BRAND_NAME}}", {"b": "{{CONSULTANT_NAME}}"}], "n": 3}
    out = brand.render_obj(data)
    assert out["a"][0] == settings.brand_name
    assert out["a"][1]["b"] == brand.consultant_name()
    assert out["n"] == 3


BRAND_TOKENS = ("{{BRAND_NAME}}", "{{CONSULTANT_NAME}}", "{{CONSULTANT_FIRST_NAME}}",
                "{{CONSULTANT_FOCUS}}", "{{DEPLOY_DOMAIN}}")


def test_every_prompt_renders_without_leftovers():
    # Only the brand tokens are rendered at load time; pipeline tokens like
    # {{FORBIDDEN_ACTIONS_JSON}} / {{SOVEREIGN_CLAUSE}} are filled per call.
    for path in sorted(PROMPT_DIR.glob("*.md")):
        rendered = load_prompt(path.name)
        for token in BRAND_TOKENS:
            assert token not in rendered, f"unrendered {token} in {path.name}"
    # the knowledge loader renders too
    rendered = _load_prompt("knowledge_synthesis.md")
    for token in BRAND_TOKENS:
        assert token not in rendered


def test_static_data_renders_without_leftovers():
    for name in ("specialities.json", "forbidden-actions.json"):
        text = str(load_static(name))
        assert "{{BRAND_NAME}}" not in text and "{{CONSULTANT_NAME}}" not in text


import pytest


@pytest.fixture(scope="module")
def client():
    # Context-managed so every request shares one persistent event loop -
    # /api/settings now reads the async DB, and ad-hoc per-test TestClients
    # would break the module-level asyncpg pool ("Event loop is closed").
    # Dispose the engine pool on BOTH sides so this module neither inherits a
    # pool bound to an earlier module's closed loop nor poisons a later one
    # (test_hub_eval.py idiom - this module sorts before the hub HTTP modules).
    import asyncio

    from app.core.db import engine as _async_engine
    asyncio.run(_async_engine.dispose(close=False))
    try:
        with TestClient(app) as c:
            yield c
    finally:
        asyncio.run(_async_engine.dispose(close=False))


def test_public_settings_endpoint(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    data = r.json()
    assert data["brand_name"] == settings.brand_name
    assert data["brand_slug"] == settings.brand_slug
    assert data["brand_color_primary"].startswith("#")
    assert data["consultant_first_name"] == brand.consultant_first_name()
    expected_keys = {"id", "label", "description", "short_label", "icon",
                     "deliverable_type", "knowledge_tags", "capabilities",
                     "complexity_baseline", "base_fee_credits"}
    assert data["specialities"] and all(
        set(s) == expected_keys for s in data["specialities"])
    for s in data["specialities"]:
        assert isinstance(s["knowledge_tags"], list)
        assert isinstance(s["capabilities"], list)
        assert s["short_label"]
        assert isinstance(s["base_fee_credits"], (int, float)) and s["base_fee_credits"] >= 0
    # Instance capability declaration (§hub capability discovery).
    assert data["capabilities"] == settings.capabilities_list
    from app.services.pricing import load_static
    raw = load_static("specialities.json")["specialities"]
    published = {s["id"] for s in data["specialities"]}
    assert published == {s["id"] for s in raw if s.get("enabled")}
    assert set(data["intake"]) == {"ai_enabled", "direct_quote_enabled", "auto_dev_enabled",
                                   "chat_enabled"}
    # chat is opt-in: paused (disabled) until the admin explicitly enables it
    assert data["intake"]["chat_enabled"] is False


def test_public_settings_intake_follows_pause_flags(client):
    """§hub capability discovery: pausing a kind's deposits flips its intake flag."""
    from sqlalchemy import delete
    from app.core.db import SyncSession
    from app.models import AppSetting
    from app.services.app_settings import PAUSE_AI

    assert client.get("/api/settings").json()["intake"]["ai_enabled"] is True
    try:
        with SyncSession() as db:
            db.add(AppSetting(key=PAUSE_AI, value=True))
            db.commit()
        intake = client.get("/api/settings").json()["intake"]
        assert intake["ai_enabled"] is False
        assert intake["direct_quote_enabled"] is True  # untouched kind unaffected
    finally:
        with SyncSession() as db:
            db.execute(delete(AppSetting).where(AppSetting.key == PAUSE_AI))
            db.commit()


def test_brand_slug_shapes():
    assert settings.brand_slug == settings.brand_name.split(".")[0].lower()
