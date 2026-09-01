"""Newer-model compatibility for the raw chat client (§llm):

- GPT-5 / o-series models reject `max_tokens` and require `max_completion_tokens`;
  the dev-build SDK (LiteLLM) translates this, so the raw client must retry too.
- The chat-intent classifier must survive an unpriced model: a metering failure
  (UnknownModelError) must not drop the customer's classified request.
- Anthropic's OpenAI-compatible surface refuses `response_format: json_object`,
  which every structured call on the platform sets.
"""
import httpx
import pytest

from app.services import llm
from app.services.pricing import UnknownModelError


class _Resp:
    def __init__(self, status_code, *, text="", json_data=None, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}",
                                        request=httpx.Request("POST", "http://x"), response=self)

    def json(self):
        return self._json


_OK = {"choices": [{"message": {"content": '{"ok": true}'}}],
       "model": "gpt-5.6-luna", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

# The verbatim shape of OpenAI's 400 when a GPT-5 model gets `max_tokens`.
_MCT_400 = ("Unsupported parameter: 'max_tokens' is not supported with this model. "
            "Use 'max_completion_tokens' instead.")


def _script(monkeypatch, responses):
    seq = iter(responses)
    posts: list[dict] = []

    def fake_post(url, **kw):
        posts.append(kw.get("json"))
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda d: None)
    return posts


def test_chat_retries_with_max_completion_tokens(monkeypatch):
    posts = _script(monkeypatch, [_Resp(400, text=_MCT_400), _Resp(200, json_data=_OK)])
    content, usage = llm.chat([{"role": "user", "content": "hi"}], model="gpt-5.6-luna",
                              base_url="http://x", api_key="k", max_tokens=300)
    assert content == '{"ok": true}'
    # first attempt used the classic param; the retry swapped in the newer one
    assert "max_tokens" in posts[0] and "max_completion_tokens" not in posts[0]
    assert posts[1]["max_completion_tokens"] == 300 and "max_tokens" not in posts[1]


def test_chat_does_not_retry_on_unrelated_400(monkeypatch):
    posts = _script(monkeypatch, [_Resp(400, text="model 'foo' does not exist")])
    with pytest.raises(llm.LLMUnavailable) as exc:
        llm.chat([{"role": "user", "content": "hi"}], model="foo",
                 base_url="http://x", api_key="k")
    assert "does not exist" in str(exc.value)  # body surfaced for observability
    assert len(posts) == 1  # no spurious retry


_FLAKY_401 = ('{"error": {"message": "You have insufficient permissions for this '
              'operation.", "type": "invalid_request_error", "param": null, "code": null}}')


def test_chat_retries_transient_insufficient_permissions(monkeypatch):
    # OpenAI's intermittent gpt-5.6 chat-completions rejection clears on retry
    posts = _script(monkeypatch, [_Resp(401, text=_FLAKY_401), _Resp(200, json_data=_OK)])
    content, _ = llm.chat([{"role": "user", "content": "hi"}], model="gpt-5.6-luna",
                          base_url="http://x", api_key="k")
    assert content == '{"ok": true}'
    assert len(posts) == 2


def test_chat_does_not_retry_other_auth_errors(monkeypatch):
    posts = _script(monkeypatch, [_Resp(401, text='{"error": {"message": "Incorrect API '
                                                  'key provided"}}')])
    with pytest.raises(llm.LLMUnavailable):
        llm.chat([{"role": "user", "content": "hi"}], model="m",
                 base_url="http://x", api_key="k")
    assert len(posts) == 1  # a genuinely bad key fails immediately


def test_chat_normal_success_keeps_max_tokens(monkeypatch):
    posts = _script(monkeypatch, [_Resp(200, json_data=_OK)])
    llm.chat([{"role": "user", "content": "hi"}], model="m", base_url="http://x",
             api_key="k", max_tokens=128)
    assert posts[0]["max_tokens"] == 128 and "max_completion_tokens" not in posts[0]


def test_classify_survives_unpriced_model(monkeypatch):
    from app.agents import pipeline

    usage = {"model": "gpt-5.6-luna", "input_tokens": 1, "output_tokens": 1}
    monkeypatch.setattr(pipeline, "chat_json", lambda *a, **k: (
        {"intent": "new_request", "request_type": "bug", "summary": "blocks not destroyed"}, usage))

    def unpriced(*a, **k):
        raise UnknownModelError("No price row for model 'gpt-5.6-luna'")

    monkeypatch.setattr(pipeline, "record_usage", unpriced)

    class _P:
        id = "p1"

    out = pipeline.classify_chat_intent(None, _P(), "ctx", "the bird doesn't destroy blocks")
    # the billing failure must NOT collapse the classification to none
    assert out == {"intent": "new_request", "request_type": "bug", "summary": "blocks not destroyed",
                   "question": None, "options": []}


_NULL_CONTENT = {"choices": [{"message": {"content": None}, "finish_reason": "length"}],
                 "model": "Qwen3.6-27B", "usage": {"prompt_tokens": 10, "completion_tokens": 600}}


def test_chat_length_retry_recovers_and_bills_both_calls(monkeypatch):
    """§reasoning headroom: an empty completion at finish_reason=length gets ONE
    retry at 4x the budget - re-sending the same call is guaranteed to fail the
    same way - and the first call's reasoning tokens (real provider spend) are
    folded into the returned usage."""
    posts = _script(monkeypatch, [_Resp(200, json_data=_NULL_CONTENT),
                                  _Resp(200, json_data=_OK)])
    content, usage = llm.chat([{"role": "user", "content": "hi"}], model="m",
                              base_url="http://x", api_key="k", max_tokens=600)
    assert content == '{"ok": true}'
    assert [p["max_tokens"] for p in posts] == [600, 2400]
    assert usage["input_tokens"] == 20 and usage["output_tokens"] == 605


def test_chat_raises_llm_unavailable_on_null_content(monkeypatch):
    """A reasoning model that burns the whole token budget answers 200 with
    content: null - after the one 4x length retry also comes back empty, chat
    must raise LLMUnavailable (the class every caller fail-safes on), never let
    a None escape to a .strip() crash downstream."""
    posts = _script(monkeypatch, [_Resp(200, json_data=_NULL_CONTENT),
                                  _Resp(200, json_data=_NULL_CONTENT)])
    with pytest.raises(llm.LLMUnavailable, match="finish_reason=length"):
        llm.chat([{"role": "user", "content": "hi"}], model="m", base_url="http://x",
                 api_key="k", max_tokens=600)
    assert [p["max_tokens"] for p in posts] == [600, 2400]


def test_classify_fail_safes_to_none_on_null_content(monkeypatch):
    from app.agents import pipeline

    _script(monkeypatch, [_Resp(200, json_data=_NULL_CONTENT),
                          _Resp(200, json_data=_NULL_CONTENT)])

    class _P:
        id = "p1"

    out = pipeline.classify_chat_intent(None, _P(), "ctx", "Please develop this issue")
    # none, so a failure still triggers no side effect - and flagged, so the
    # caller can tell an outage from "the model read it and there is nothing to
    # do" and refuse to let the answering model narrate the first one.
    assert out == {"intent": "none", "unavailable": True}


_EFFORT_400 = "Unknown parameter: 'reasoning_effort'."


def test_chat_sends_effort_and_strips_on_rejection(monkeypatch):
    posts = _script(monkeypatch, [_Resp(200, json_data=_OK)])
    llm.chat([{"role": "user", "content": "hi"}], model="m", base_url="http://x",
             api_key="k", max_tokens=64, effort="low")
    assert posts[0]["reasoning_effort"] == "low"

    posts = _script(monkeypatch, [_Resp(400, text=_EFFORT_400), _Resp(200, json_data=_OK)])
    llm.chat([{"role": "user", "content": "hi"}], model="m", base_url="http://x",
             api_key="k", max_tokens=64, effort="low")
    assert "reasoning_effort" in posts[0] and "reasoning_effort" not in posts[1]


def test_chat_no_effort_param_when_unset(monkeypatch):
    posts = _script(monkeypatch, [_Resp(200, json_data=_OK)])
    llm.chat([{"role": "user", "content": "hi"}], model="m", base_url="http://x", api_key="k")
    assert "reasoning_effort" not in posts[0]


def test_chat_sends_cache_key_only_to_supporting_hosts(monkeypatch):
    # Mistral: prompt caching is opt-in via prompt_cache_key (§18) - sent.
    posts = _script(monkeypatch, [_Resp(200, json_data=_OK)])
    llm.chat([{"role": "user", "content": "hi"}], model="m",
             base_url="https://api.mistral.ai/v1", api_key="k", cache_key="proj-1")
    assert posts[0]["prompt_cache_key"] == "proj-1"

    # Unknown OpenAI-compatible gateway: never sent (strict ones 400 on it).
    posts = _script(monkeypatch, [_Resp(200, json_data=_OK)])
    llm.chat([{"role": "user", "content": "hi"}], model="m",
             base_url="https://llm.example.net/v1", api_key="k", cache_key="proj-1")
    assert "prompt_cache_key" not in posts[0]

    # A supporting host that still rejects it: stripped and retried once.
    posts = _script(monkeypatch, [
        _Resp(400, text="Unknown parameter: 'prompt_cache_key'."),
        _Resp(200, json_data=_OK)])
    llm.chat([{"role": "user", "content": "hi"}], model="m",
             base_url="https://api.mistral.ai/v1", api_key="k", cache_key="proj-1")
    assert "prompt_cache_key" in posts[0] and "prompt_cache_key" not in posts[1]


# ------------------------------------------------- response_format (Anthropic)

# The verbatim body Anthropic's OpenAI-compatible surface returns for
# `response_format: {"type": "json_object"}` - captured from production on
# 2026-08-30, where it silently broke every structured call on the instance.
_JSON_400 = ('{"error":{"code":"invalid_request_error","message":'
             '"response_format.type: Input should be \'json_schema\'",'
             '"type":"invalid_request_error","param":null}}')


def test_json_mode_retries_without_response_format(monkeypatch):
    """Every structured call the platform makes sets json_mode, so an endpoint
    that refuses `json_object` takes out the chat classifier, the evaluation, the
    request titles and the plan gate at once - silently, because each caller
    fail-safes. The prompts already specify their JSON shape."""
    posts = _script(monkeypatch, [_Resp(400, text=_JSON_400), _Resp(200, json_data=_OK)])
    content, _ = llm.chat([{"role": "user", "content": "hi"}], model="claude-sonnet-5",
                          base_url="http://x", api_key="k", json_mode=True)
    assert content == '{"ok": true}'
    assert posts[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in posts[1]
    assert posts[1]["messages"] == posts[0]["messages"]  # nothing else changed


def test_chat_json_survives_the_refusal_end_to_end(monkeypatch):
    _script(monkeypatch, [_Resp(400, text=_JSON_400), _Resp(200, json_data=_OK)])
    result, _ = llm.chat_json([{"role": "user", "content": "hi"}],
                              model="claude-sonnet-5", base_url="http://x", api_key="k")
    assert result == {"ok": True}


def test_a_400_that_names_nothing_we_know_still_raises(monkeypatch):
    """The strip-and-retry is keyed on the provider naming the parameter, so an
    unrelated 400 must not be retried into a second charge."""
    posts = _script(monkeypatch, [_Resp(400, text="model not found")])
    with pytest.raises(llm.LLMUnavailable):
        llm.chat([{"role": "user", "content": "hi"}], model="nope",
                 base_url="http://x", api_key="k", json_mode=True)
    assert len(posts) == 1


def test_a_plain_call_never_sends_response_format(monkeypatch):
    posts = _script(monkeypatch, [_Resp(200, json_data=_OK)])
    llm.chat([{"role": "user", "content": "hi"}], model="m", base_url="http://x",
             api_key="k")
    assert "response_format" not in posts[0]
