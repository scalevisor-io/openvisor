"""Admin management of saved LLM API endpoints (§model config).

Instance-admin-level (the spoke owner's, not per customer org): one global list of
reusable OpenAI-compatible endpoints + credentials that a project's Model config
modal selects from. `provider` is a preset hint (openai|anthropic|mistral|openrouter|eurouter|carouter|custom)
for the UI badge + base-URL prefill. The API key is envelope-encrypted at rest and
NEVER returned (only `has_api_key: bool`), so no secret is readable back out of the
API. Deleting an endpoint still referenced by a project, a program or a program
instance is refused (409) so a build's model config can never silently fall back
to the global default.
"""
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import require_admin
from app.core.encryption import decrypt, encrypt
from app.models import ModelEndpoint, Program, ProgramInstance, ProjectModelConfig
from app.schemas.schemas import ModelCatalogIn, ModelEndpointIn, ModelEndpointPatchIn
from app.services import pricing

router = APIRouter(prefix="/api/admin/model-endpoints", tags=["model-endpoints"],
                   dependencies=[Depends(require_admin)])

_HTTP_RE = re.compile(r"^https?://", re.I)


def _out(ep: ModelEndpoint) -> dict:
    """Serialize an endpoint for the admin UI - the API key is never exposed, only
    whether one is set."""
    return {
        "id": ep.id,
        "label": ep.label,
        "provider": ep.provider,
        "base_url": ep.base_url,
        "model_name": ep.model_name,
        "input_price": ep.input_price,
        "output_price": ep.output_price,
        "cached_input_price": ep.cached_input_price,
        # true when the model is billable from the static price table, so the UI
        # can hide/optional-ize the custom-price fields.
        "model_priced": bool(ep.model_name) and pricing.is_priced(ep.model_name),
        "reasoning_effort": ep.reasoning_effort,
        "supports_images": ep.supports_images,
        "supports_images_source": ep.supports_images_source,
        "has_api_key": bool(ep.api_key_enc),
        "created_at": ep.created_at,
    }


def _validate_base_url(base_url: str) -> str:
    url = base_url.strip()
    if not _HTTP_RE.match(url):
        raise HTTPException(400, "Base URL must start with http:// or https://")
    return url


def _resolve_pricing(model_name: str, input_price: float | None,
                     output_price: float | None, cached_input_price: float | None,
                     ) -> tuple[str, float | None, float | None, float | None]:
    """Validate the model + its pricing. A model in the static price table needs no
    custom price (the table wins, so any supplied price is dropped). A model NOT in
    the table must carry an admin-supplied input+output price per 1M tokens, which
    services/llm.record_usage bills against - otherwise it's unbillable (OCPA: never
    bill 0). cached_input_price is the optional prompt-cache-read rate (§18); null
    = no discount, and it can never exceed the input price."""
    model = (model_name or "").strip()
    if not model:
        raise HTTPException(400, "A model name is required")
    if pricing.is_priced(model):
        return model, None, None, None
    if input_price is None or output_price is None:
        raise HTTPException(
            400, f"Model '{model}' isn't in the price table - enter its input and "
                 "output cost per 1M tokens so its usage can be billed")
    if cached_input_price is not None and cached_input_price > float(input_price):
        raise HTTPException(
            400, "The cached-input price cannot exceed the input price")
    return (model, float(input_price), float(output_price),
            None if cached_input_price is None else float(cached_input_price))


@router.get("")
async def list_endpoints(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(ModelEndpoint).order_by(ModelEndpoint.created_at))).scalars().all()
    return [_out(e) for e in rows]


@router.post("")
async def create_endpoint(body: ModelEndpointIn, db: AsyncSession = Depends(get_db)):
    model, inp, outp, cached = _resolve_pricing(body.model_name, body.input_price,
                                                body.output_price, body.cached_input_price)
    ep = ModelEndpoint(
        label=body.label.strip(),
        provider=body.provider,
        base_url=_validate_base_url(body.base_url),
        api_key_enc=encrypt(body.api_key),
        model_name=model,
        input_price=inp,
        output_price=outp,
        cached_input_price=cached,
        reasoning_effort=body.reasoning_effort,
        supports_images=body.supports_images,
        supports_images_source=("admin" if body.supports_images is not None else None),
    )
    db.add(ep)
    await db.commit()
    await db.refresh(ep)
    return _out(ep)


@router.patch("/{endpoint_id}")
async def update_endpoint(endpoint_id: str, body: ModelEndpointPatchIn,
                          db: AsyncSession = Depends(get_db)):
    ep = await db.get(ModelEndpoint, endpoint_id)
    if ep is None:
        raise HTTPException(404, "Model endpoint not found")
    if body.label is not None:
        ep.label = body.label.strip()
    if body.provider is not None:
        ep.provider = body.provider
    if body.base_url is not None:
        ep.base_url = _validate_base_url(body.base_url)
    # Re-resolve the model + pricing whenever any of them is touched (the effective
    # model may become unbillable, requiring a supplied price).
    if (body.model_name is not None or body.input_price is not None
            or body.output_price is not None or body.cached_input_price is not None):
        model = body.model_name if body.model_name is not None else ep.model_name
        inp = body.input_price if body.input_price is not None else ep.input_price
        outp = body.output_price if body.output_price is not None else ep.output_price
        cached = (body.cached_input_price if body.cached_input_price is not None
                  else ep.cached_input_price)
        (ep.model_name, ep.input_price, ep.output_price,
         ep.cached_input_price) = _resolve_pricing(model, inp, outp, cached)
    if body.reasoning_effort is not None:
        ep.reasoning_effort = body.reasoning_effort or None  # "" resets to default
    if body.supports_images is not None:
        # An admin declaration outranks the probe and survives later Tests: they
        # are saying they know the model reads images even if the probe can't
        # confirm it (a gateway that rejects the tiny PNG, an unusual API shape).
        ep.supports_images = body.supports_images
        ep.supports_images_source = "admin"
    if body.api_key:  # blank/None → keep the existing key
        ep.api_key_enc = encrypt(body.api_key)
    await db.commit()
    await db.refresh(ep)
    return _out(ep)


# Non-chat model families we hide from the picked list (embeddings, audio, image,
# moderation…): the endpoint feeds the dev agent, which only ever chats. Best-effort
# - a miss just leaves a useless option in the dropdown.
_NON_CHAT_RE = re.compile(
    r"embed|whisper|tts|dall-e|moderation|audio|realtime|transcribe|image|ocr"
    r"|babbage|davinci", re.I)


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _auth_headers(provider: str, api_key: str) -> dict:
    # Anthropic's native API authenticates with x-api-key; every other provider here
    # (and Anthropic's own OpenAI-compat layer, which the runner uses) takes Bearer.
    if provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {api_key}"}


async def _resolve_key(db: AsyncSession, api_key: str | None,
                       endpoint_id: str | None) -> str:
    """The catalog/test key: the one typed into the form, else the stored (encrypted)
    key of the endpoint being edited - the UI can never read a saved key back."""
    if api_key:
        return api_key
    ep = await db.get(ModelEndpoint, endpoint_id) if endpoint_id else None
    if ep is None or not ep.api_key_enc:
        raise HTTPException(400, "Provide an API key (or an existing endpoint id)")
    return decrypt(ep.api_key_enc)


# §chat images: the discovery probe. /models advertises ids, never capabilities,
# so the only honest way to know whether a model reads images is to send it one.
# A 1x1 transparent PNG keeps the probe near-free in tokens.
VISION_PROBE_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf"
                    "FcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
_VISION_REJECT_HINTS = ("image", "vision", "multimodal", "content type", "image_url",
                        "not support")


@router.post("/models")
async def list_provider_models(body: ModelCatalogIn, db: AsyncSession = Depends(get_db)):
    """Pull the provider's live model list (`GET /models` - same shape on OpenAI,
    Mistral and Anthropic) so the endpoint form offers a picker instead of a blind
    text field. Soft-fails to an empty list + error so the form can always fall back
    to a custom model name; each model carries `priced` (in the static price table →
    no custom price needed)."""
    base = _validate_base_url(body.base_url)
    key = await _resolve_key(db, body.api_key, body.endpoint_id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{base.rstrip('/')}/models",
                                 headers=_auth_headers(body.provider, key))
        if r.status_code != 200:
            return {"models": [], "error": f"HTTP {r.status_code}: "
                                           f"{_redact(r.text[:300], key)}"}
        ids = sorted({m.get("id", "") for m in r.json().get("data", [])} - {""})
    except Exception as exc:  # noqa: BLE001 - network errors surface as a soft error
        return {"models": [], "error": _redact(str(exc)[:300], key)}
    return {"models": [{"id": m, "priced": pricing.is_priced(m)}
                       for m in ids if not _NON_CHAT_RE.search(m)],
            "error": None}


@router.get("/priced-models")
async def priced_models():
    """Every api_model the static price table can bill - the form checks a custom
    (hand-typed) model name against it to decide whether to require prices."""
    return {"models": sorted(pricing.priced_models())}


async def _probe(client: httpx.AsyncClient, url: str, headers: dict,
                 payload: dict, key: str) -> dict:
    try:
        r = await client.post(url, headers=headers, json=payload)
        return {"ok": r.is_success, "status": r.status_code,
                "error": None if r.is_success else _redact(r.text[:300], key)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "error": _redact(str(exc)[:300], key)}


@router.post("/{endpoint_id}/test")
async def test_endpoint(endpoint_id: str, db: AsyncSession = Depends(get_db)):
    """Preflight the exact API surfaces a dev run will hit: `/chat/completions`
    always (agent + condenser path), and `/responses` for OpenAI gpt-5/codex-family
    models (the OpenHands SDK routes agent steps there). A 1-token ping per surface;
    results are informative, never blocking - a provider-side blip shouldn't wedge
    endpoint management."""
    ep = await db.get(ModelEndpoint, endpoint_id)
    if ep is None:
        raise HTTPException(404, "Model endpoint not found")
    if not ep.model_name:
        raise HTTPException(400, "Set a model name first")
    key = decrypt(ep.api_key_enc)
    base = ep.base_url.rstrip("/")
    headers = _auth_headers(ep.provider, key)
    chat_payload = {"model": ep.model_name,
                    "messages": [{"role": "user", "content": "ping"}]}
    # OpenAI dropped max_tokens on newer models; compat servers may not know the
    # replacement. Try the provider-appropriate cap, then once with the other.
    caps = ("max_completion_tokens", "max_tokens") if ep.provider == "openai" \
        else ("max_tokens", "max_completion_tokens")
    async with httpx.AsyncClient(timeout=30) as client:
        chat = await _probe(client, f"{base}/chat/completions", headers,
                            {**chat_payload, caps[0]: 16}, key)
        if not chat["ok"] and chat["status"] == 400 and caps[0] in (chat["error"] or ""):
            chat = await _probe(client, f"{base}/chat/completions", headers,
                                {**chat_payload, caps[1]: 16}, key)
        responses = None
        if ep.provider == "openai" and (ep.model_name.startswith("gpt-5")
                                        or "codex" in ep.model_name):
            responses = await _probe(
                client, f"{base}/responses", headers,
                {"model": ep.model_name, "input": "ping", "max_output_tokens": 16}, key)
        effort = None
        if ep.reasoning_effort:
            # Empirical acceptance check for the configured effort: there is no
            # capability-discovery API in the OpenAI-compatible contract, so the
            # 1-token probe IS the discovery. No strip-retry here - the point is
            # to learn whether the provider honors the parameter.
            cap = caps[0] if chat["ok"] or caps[0] not in (chat.get("error") or "") else caps[1]
            e = await _probe(client, f"{base}/chat/completions", headers,
                             {**chat_payload, cap: 16,
                              "reasoning_effort": ep.reasoning_effort}, key)
            rejected = (not e["ok"] and e["status"] == 400
                        and "reasoning" in (e["error"] or "").lower())
            effort = {"value": ep.reasoning_effort,
                      "accepted": bool(e["ok"]),
                      "detail": ("accepted" if e["ok"] else
                                 "rejected - calls fall back to the provider default"
                                 if rejected else (e["error"] or "unreachable"))}
        # §chat images: only probe when the basic chat surface answered - a
        # provider that is down would otherwise be recorded as "no images".
        vision = None
        if chat["ok"]:
            cap = caps[0] if caps[0] not in (chat.get("error") or "") else caps[1]
            v = await _probe(client, f"{base}/chat/completions", headers,
                             {"model": ep.model_name, cap: 16,
                              "messages": [{"role": "user", "content": [
                                  {"type": "text", "text": "ping"},
                                  {"type": "image_url",
                                   "image_url": {"url": VISION_PROBE_PNG}}]}]}, key)
            err = (v["error"] or "").lower()
            if v["ok"]:
                verdict, detail = True, "accepted - image attachments are enabled"
            elif v["status"] == 400 and any(h in err for h in _VISION_REJECT_HINTS):
                verdict, detail = False, "rejected - this model can't read images"
            else:
                # An unrelated failure (rate limit, gateway hiccup): leave the
                # stored verdict alone rather than recording a false negative.
                verdict, detail = None, (v["error"] or "unreachable")
            if verdict is not None and ep.supports_images_source != "admin":
                ep.supports_images = verdict
                ep.supports_images_source = "probe"
                await db.commit()
            vision = {"supported": verdict, "detail": detail,
                      "source": ep.supports_images_source}
    return {"chat_completions": chat, "responses": responses, "effort": effort,
            "vision": vision}


@router.delete("/{endpoint_id}")
async def delete_endpoint(endpoint_id: str, db: AsyncSession = Depends(get_db)):
    ep = await db.get(ModelEndpoint, endpoint_id)
    if ep is None:
        raise HTTPException(404, "Model endpoint not found")
    used = (await db.execute(
        select(func.count()).select_from(ProjectModelConfig).where(
            ProjectModelConfig.endpoint_id == endpoint_id))).scalar_one()
    prog_used = (await db.execute(
        select(func.count()).select_from(Program).where(
            Program.model_endpoint_id == endpoint_id))).scalar_one()
    # §28 per-instance model: a customer can pin their own instance to an
    # endpoint, so instances hold references the program row doesn't show.
    inst_used = (await db.execute(
        select(func.count()).select_from(ProgramInstance).where(
            ProgramInstance.model_endpoint_id == endpoint_id))).scalar_one()
    if used or prog_used or inst_used:
        parts = ([f"{used} project(s)"] if used else []) + (
            [f"{prog_used} program(s)"] if prog_used else []) + (
            [f"{inst_used} program instance(s)"] if inst_used else [])
        raise HTTPException(
            409, f"In use by {' and '.join(parts)} - reassign their model config first")
    await db.delete(ep)
    await db.commit()
    return {"ok": True}
