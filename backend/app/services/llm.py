"""OpenAI-compatible chat client with token metering (sync - called from
Celery tasks; the API never blocks on model calls)."""
import json
import logging
import time

import httpx
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import CreditTransaction, Organization, Project, Request
from app.services import model_prices
from app.services.pricing import cost_credits

log = logging.getLogger(__name__)

# Transient failures worth retrying: provider rate limits (429) and gateway/
# server errors (5xx). A 4xx other than 429 is a real client error - never retry,
# with ONE exception: OpenAI intermittently rejects gpt-5.6-family chat completions
# with a spurious 401/403 "insufficient permissions" on keys that just worked
# (provider-side bug, 2026-07 - observed clearing on immediate retry). That exact
# signature is treated as transient; any other auth error still fails immediately.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_AUTH_HINT = "insufficient permissions for this operation"


def _transient_auth(resp: httpx.Response) -> bool:
    if resp.status_code not in (401, 403):
        return False
    try:
        return _TRANSIENT_AUTH_HINT in resp.text.lower()
    except Exception:  # noqa: BLE001 - unreadable body -> not the known signature
        return False


class LLMUnavailable(Exception):
    pass


def _backoff(attempt: int) -> float:
    """Exponential backoff (1, 2, 4, 8 ... s) capped so a run can't stall for
    minutes on a wedged provider."""
    return min(2.0 ** attempt, 30.0)


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Honor the provider's Retry-After header (seconds) on a 429, else fall
    back to exponential backoff."""
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return _backoff(attempt)


def _post_chat(url: str, payload: dict, api_key: str) -> httpx.Response:
    """POST to the chat endpoint with bounded retries on rate limits / 5xx /
    transport errors (exponential backoff, Retry-After honored). Raises
    LLMUnavailable once retries are exhausted - the caller's contract is
    unchanged, so is_local heuristic fallbacks still fire, but a transient 429
    no longer degrades the very first project evaluation."""
    headers = {"Authorization": f"Bearer {api_key}"}
    attempts = max(settings.llm_max_retries, 0) + 1
    for i in range(attempts):
        last = i == attempts - 1
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=120)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if last:
                raise LLMUnavailable(str(exc)) from exc
            delay = _backoff(i)
            log.warning("LLM transport error (%s); retry %d/%d in %.1fs",
                        exc, i + 1, attempts - 1, delay)
            time.sleep(delay)
            continue
        if (resp.status_code in _RETRY_STATUSES or _transient_auth(resp)) and not last:
            delay = _retry_after(resp, i)
            log.warning("LLM HTTP %s; retry %d/%d in %.1fs",
                        resp.status_code, i + 1, attempts - 1, delay)
            time.sleep(delay)
            continue
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Include the provider's error body: a 4xx reason (e.g. "use
            # max_completion_tokens", an invalid model) is otherwise invisible in
            # the logs, and chat() inspects it to retry with the newer parameter.
            body = ""
            try:
                body = resp.text[:600]
            except Exception:
                pass
            raise LLMUnavailable(f"{exc}: {body}".strip()) from exc
        return resp
    raise LLMUnavailable("LLM retries exhausted")  # pragma: no cover - loop always returns/raises


# GPT-5 / o-series models reject the classic `max_tokens` parameter and require
# `max_completion_tokens`; their 400 body names the required parameter. LiteLLM
# (used by the dev-build SDK) translates this transparently - our raw client must
# too, or every pipeline call routed to such a model 400s.
_MAX_COMPLETION_HINT = "max_completion_tokens"

# Providers where sending prompt_cache_key is known-safe: Mistral requires it to
# opt IN to prompt caching (cache reads then come back in usage and bill at the
# §18 cached rate); OpenAI accepts it as an official cache-routing hint. Other
# OpenAI-compatible gateways may 400 on unknown params, so it is never sent to
# them (and a rejection is stripped-and-retried anyway, like reasoning_effort).
_CACHE_KEY_HOSTS = {"api.mistral.ai", "api.openai.com"}


def _supports_cache_key(base: str) -> bool:
    from urllib.parse import urlparse
    try:
        return (urlparse(base).hostname or "") in _CACHE_KEY_HOSTS
    except ValueError:
        return False


def chat(messages: list[dict], *, base_url: str | None = None, api_key: str | None = None,
         model: str | None = None, json_mode: bool = False, max_tokens: int = 2048,
         effort: str | None = None, cache_key: str | None = None) -> tuple[str, dict]:
    """Returns (content, usage). usage = {model, input_tokens, output_tokens,
    cached_input_tokens}. `effort` (§effort) requests a reasoning-effort level;
    providers that reject the parameter get one retry without it, so callers can
    pass it blindly. `cache_key` (§18) is a stable conversation/project id sent as
    `prompt_cache_key` on supporting providers so repeated prefixes are served
    (and billed) from the provider's prompt cache; callers pass it blindly too."""
    base = (base_url or settings.openai_base_url).rstrip("/")
    url = f"{base}/chat/completions"
    key = api_key or settings.openai_api_key
    send_cache_key = bool(cache_key) and _supports_cache_key(base)

    def _payload(token_param: str, with_effort: bool = True, with_cache: bool = True) -> dict:
        p: dict = {"model": model or settings.openai_model, "messages": messages,
                   token_param: max_tokens}
        if json_mode:
            p["response_format"] = {"type": "json_object"}
        if effort and with_effort:
            p["reasoning_effort"] = effort
        if send_cache_key and with_cache:
            p["prompt_cache_key"] = cache_key
        return p

    def _post(token_param: str):
        try:
            return _post_chat(url, _payload(token_param), key)
        except LLMUnavailable as exc:
            # A provider that doesn't know reasoning_effort or prompt_cache_key
            # 400s naming it - strip the named one and retry once; anything else
            # re-raises unchanged.
            if effort and "reasoning" in str(exc).lower():
                return _post_chat(url, _payload(token_param, with_effort=False), key)
            if send_cache_key and "prompt_cache_key" in str(exc):
                return _post_chat(url, _payload(token_param, with_cache=False), key)
            raise

    try:
        resp = _post("max_tokens")
    except LLMUnavailable as exc:
        # Retry once with `max_completion_tokens` when the model demands it (GPT-5 /
        # o-series). Only when the provider's 400 explicitly names it, so any other
        # error re-raises unchanged.
        if _MAX_COMPLETION_HINT not in str(exc):
            raise
        resp = _post("max_completion_tokens")
    data = resp.json()
    choice = data["choices"][0]
    content = choice["message"]["content"]
    if content is None:
        # Reasoning models (Qwen3, GPT-5/o-series) can exhaust the whole token
        # budget on hidden reasoning and answer 200 with content: null. Raise the
        # class every caller already fail-safes on instead of crashing downstream.
        raise LLMUnavailable(
            f"empty completion (finish_reason={choice.get('finish_reason')}) - "
            "the model likely spent the entire token budget on reasoning")
    raw_usage = data.get("usage", {}) or {}
    usage = {
        "model": data.get("model") or model or settings.openai_model,
        "input_tokens": raw_usage.get("prompt_tokens", 0),
        "output_tokens": raw_usage.get("completion_tokens", 0),
        # Prompt-cache READS (subset of prompt_tokens), billed at the cached rate
        # (§18): OpenAI-compatible `prompt_tokens_details.cached_tokens`, with the
        # flat `cached_tokens` fallback some providers use.
        "cached_input_tokens": int(
            (raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or raw_usage.get("cached_tokens") or 0),
    }
    return content, usage


def chat_json(messages: list[dict], **kw) -> tuple[dict, dict]:
    content, usage = chat(messages, json_mode=True, **kw)
    # tolerate fenced output
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):text.rfind("}") + 1]
    return json.loads(text), usage


def _endpoint_price(db: Session, api_model: str) -> tuple[float, float, float | None] | None:
    """Admin-supplied price for a model NOT in the static table, so a project
    routed to a model the platform doesn't yet know how to bill is still metered.
    Resolution lives in `model_prices` - billing and every display surface must
    read the same two sources."""
    return model_prices.for_model(db, api_model)


def record_usage(db: Session, project: Project, usage: dict, detail: str,
                 request: Request | None = None) -> float:
    """Meter a model call against the org wallet (PROMPT §14.6/§18). When the
    call belongs to a customer Request's thread (scoped dev run, classifier
    reply), pass it so the usage is also attributed to that request. Returns
    credits charged. Caller commits."""
    credits = cost_credits(usage["model"], usage["input_tokens"], usage["output_tokens"],
                           price=_endpoint_price(db, usage["model"]),
                           cached_input_tokens=usage.get("cached_input_tokens", 0))
    tokens = usage["input_tokens"] + usage["output_tokens"]
    project.tokens_consumed = (project.tokens_consumed or 0) + tokens
    project.cost_credits = (project.cost_credits or 0.0) + credits
    if request is not None:
        request.tokens_consumed = (request.tokens_consumed or 0) + tokens
        request.cost_credits = (request.cost_credits or 0.0) + credits
    _debit_org(db, project.org_id, credits)
    db.add(CreditTransaction(org_id=project.org_id, project_id=project.id,
                             amount=-credits, kind="consumption", detail=detail,
                             tokens=tokens,
                             tokens_cached=usage.get("cached_input_tokens") or None))
    return credits


def _debit_org(db: Session, org_id: str, credits: float) -> None:
    # §parallel-builds MR2: atomic decrement - N concurrent runs/answers must
    # never lose a debit to a read-modify-write race. The expire keeps any
    # already-loaded ORM instance honest for later same-session reads.
    db.execute(update(Organization).where(Organization.id == org_id)
               .values(credit_balance=func.coalesce(Organization.credit_balance, 0.0)
                       - credits))
    org = db.get(Organization, org_id)
    if org is not None:
        db.expire(org, ["credit_balance"])


def record_project_usage(db: Session, project: Project, usages: list[dict],
                         detail: str) -> float:
    """record_usage over several model calls that form ONE billable unit (a chat
    answer: query embedding + synthesis + optional rewrite), summed into a single
    consumption ledger row so the customer ledger stays one-line-per-answer.
    Returns credits charged. Caller commits."""
    credits = sum(
        cost_credits(u["model"], u["input_tokens"], u["output_tokens"],
                     price=_endpoint_price(db, u["model"]),
                     cached_input_tokens=u.get("cached_input_tokens", 0))
        for u in usages
    )
    tokens = sum(u["input_tokens"] + u["output_tokens"] for u in usages)
    project.tokens_consumed = (project.tokens_consumed or 0) + tokens
    project.cost_credits = (project.cost_credits or 0.0) + credits
    _debit_org(db, project.org_id, credits)
    db.add(CreditTransaction(org_id=project.org_id, project_id=project.id,
                             amount=-credits, kind="consumption", detail=detail,
                             tokens=tokens,
                             tokens_cached=sum(u.get("cached_input_tokens", 0)
                                               for u in usages) or None))
    return credits


def record_org_usage(db: Session, org_id: str, usages: list[dict], detail: str,
                     kind: str = "mcp_query", markup: float | None = None) -> float:
    """Org-scoped sibling of record_usage: meter one or more model calls that
    aren't tied to a project (e.g. an MCP knowledge query, a program run)
    against the org wallet. Sums cost across all usages, debits the balance,
    writes a single consumption ledger row (project_id NULL). markup=None uses
    the global CREDIT_MARKUP (per-program markups replace it). Returns credits
    charged. Caller commits. cost_credits fails loud on an unpriced model -
    never bills 0."""
    credits = sum(
        cost_credits(u["model"], u["input_tokens"], u["output_tokens"], markup=markup,
                     price=_endpoint_price(db, u["model"]),
                     cached_input_tokens=u.get("cached_input_tokens", 0))
        for u in usages
    )
    _debit_org(db, org_id, credits)
    db.add(CreditTransaction(org_id=org_id, project_id=None, amount=-credits, kind=kind,
                             detail=detail,
                             tokens=sum(u["input_tokens"] + u["output_tokens"] for u in usages),
                             tokens_cached=sum(u.get("cached_input_tokens", 0)
                                               for u in usages) or None))
    return credits
