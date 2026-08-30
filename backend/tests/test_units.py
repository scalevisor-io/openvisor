"""Unit tests that run without external services: encryption, status machine,
pricing, altcha challenge shape."""
import pytest

from app.core.encryption import decrypt, encrypt
from app.services import github
from app.services.altcha import create_challenge
from app.services.pricing import UnknownModelError, cost_credits
from app.services.statuses import can_transition, emails_for


def test_envelope_encryption_roundtrip():
    secret = "scw-api-key-12345 ünïcode"
    blob = encrypt(secret)
    assert blob != secret and ":" in blob
    assert decrypt(blob) == secret
    # two encryptions of the same value use different data keys
    assert encrypt(secret) != blob


def test_status_machine_rules():
    assert can_transition("draft", "awaiting_review", "customer")
    assert not can_transition("draft", "development", "customer")
    assert can_transition("payment_due", "development", "system")
    # customer can always require the consultant's review
    assert can_transition("development", "awaiting_admin", "customer")
    assert can_transition("finished", "awaiting_admin", "customer")
    # the agent can only defer or request input
    assert can_transition("development", "awaiting_customer", "agent")
    assert can_transition("development", "awaiting_admin", "agent")
    assert not can_transition("awaiting_review", "payment_due", "agent")
    assert not can_transition("payment_due", "development", "agent")
    # admin can move anywhere
    assert can_transition("finished", "development", "admin")
    # the customer accepts delivery from awaiting_customer, but the agent can't
    assert can_transition("awaiting_customer", "finished", "customer")
    assert can_transition("awaiting_customer", "finished", "admin")
    assert not can_transition("awaiting_customer", "finished", "agent")
    assert not can_transition("development", "finished", "customer")


def test_email_plan():
    assert emails_for("development", "awaiting_admin").to_admin
    assert emails_for("draft", "awaiting_review").to_admin
    assert emails_for("draft", "awaiting_review").to_customer
    assert emails_for("development", "awaiting_customer").to_customer
    assert not emails_for("payment_due", "development").to_admin
    assert not emails_for("payment_due", "development").to_customer
    # the customer is always told payment is due, from either source state
    assert emails_for("awaiting_review", "payment_due").to_customer
    assert emails_for("awaiting_admin", "payment_due").to_customer
    # delivery acceptance notifies the customer
    assert emails_for("awaiting_customer", "finished").to_customer


def test_github_repo_parsing():
    assert github.is_github("git@github.com:acme/openvisor-demo-todo.git")
    assert not github.is_github("ssh://git@gitlab.example.com:10022/acme/x.git")
    assert github.parse_repo("git@github.com:acme/openvisor-demo-todo.git") == (
        "acme", "openvisor-demo-todo")
    assert github.parse_repo("https://github.com/acme/widgets") == ("acme", "widgets")


def test_pricing_known_and_unknown_models():
    from app.core.config import settings
    credits = cost_credits("mistral-large-latest", 1_000_000, 1_000_000)
    # (0.50 + 1.50) * the configured markup (CREDIT_MARKUP is operator-set,
    # so don't hardcode the .env.example value)
    assert credits == pytest.approx(2.0 * settings.credit_markup)
    with pytest.raises(UnknownModelError):
        cost_credits("mystery-model-9000", 100, 100)


def test_pricing_bills_each_cache_tier_at_its_own_rate():
    """§18: input_tokens is the WHOLE prompt and the cache counters are disjoint
    slices of it. Writes were folded into plain input until 2026-08-30, which
    under-billed every cache-heavy run."""
    from app.core.config import settings
    m = settings.credit_markup
    # claude-sonnet-5: 2.00 in / 0.20 read / 2.50 write-5m / 4.00 write-1h / 10.00 out
    credits = cost_credits("claude-sonnet-5", 1_000_000, 0,
                           cached_input_tokens=700_000,
                           cache_write_tokens=100_000,
                           cache_write_1h_tokens=100_000)
    fresh = 100_000  # whatever the three slices leave over
    assert credits == pytest.approx(
        (fresh * 2.00 + 700_000 * 0.20 + 100_000 * 2.50 + 100_000 * 4.00) / 1e6 * m)


def test_the_1h_write_tier_costs_more_than_the_5m_one():
    """The distinction the fix exists for: this harness writes on the 1-hour TTL,
    so pricing both tiers at 1.25x would have left the under-bill in place."""
    same = dict(cached_input_tokens=0)
    five = cost_credits("claude-sonnet-5", 1_000_000, 0, cache_write_tokens=1_000_000,
                        **same)
    hour = cost_credits("claude-sonnet-5", 1_000_000, 0,
                        cache_write_1h_tokens=1_000_000, **same)
    plain = cost_credits("claude-sonnet-5", 1_000_000, 0, **same)
    assert plain < five < hour
    assert five == pytest.approx(plain * 1.25)
    assert hour == pytest.approx(plain * 2.0)


def test_a_row_without_write_rates_bills_exactly_as_before():
    """The fallback chain (1h -> 5m -> base) means adding the counters can never
    change what an unpriced-for-writes model bills, and never raises."""
    from app.core.config import settings
    m = settings.credit_markup
    # mistral-large-latest has cached_input but no cache_write rates
    with_writes = cost_credits("mistral-large-latest", 1_000_000, 0,
                               cache_write_tokens=400_000,
                               cache_write_1h_tokens=100_000)
    assert with_writes == pytest.approx(0.50 * m)  # all of it at the input rate
    # an endpoint override may carry the write rates as elements 4 and 5
    ep = cost_credits("off-table-model", 1_000_000, 0, price=(1.0, 4.0, 0.1, 1.25, 2.0),
                      cache_write_1h_tokens=1_000_000)
    assert ep == pytest.approx(2.0 * m)


def test_cache_counters_can_never_bill_more_than_the_prompt():
    """Defensive: the slices are clamped in order against what is left, so
    counters that disagree with input_tokens cannot inflate a bill."""
    from app.core.config import settings
    m = settings.credit_markup
    credits = cost_credits("claude-sonnet-5", 1_000_000, 0,
                           cached_input_tokens=900_000,
                           cache_write_tokens=900_000,      # only 100k left for it
                           cache_write_1h_tokens=900_000)   # nothing left at all
    assert credits == pytest.approx((900_000 * 0.20 + 100_000 * 2.50) / 1e6 * m)


def test_the_first_production_claude_run_now_bills_what_anthropic_charged():
    """Run 7608b269 (2026-08-30), the harness's first real build. Anthropic's own
    total_cost_usd was 1.239593; the table billed it as if every written token
    were fresh input. The gap IS the bug."""
    from app.core.config import settings
    m = settings.credit_markup
    total_in, reads, out = 3_926_477, 3_818_168, 19_370
    writes_1h, fresh = 32_821, 75_488  # the split Anthropic's own cost implies

    before = cost_credits("claude-sonnet-5", total_in, out, markup=1.0,
                          cached_input_tokens=reads)
    after = cost_credits("claude-sonnet-5", total_in, out, markup=1.0,
                         cached_input_tokens=reads, cache_write_1h_tokens=writes_1h)
    assert before < after                       # the fix bills MORE, as it must
    assert after == pytest.approx(1.2395936)   # ...and matches the provider exactly
    assert before == pytest.approx(1.1739516)  # 5.3% short of it before
    assert m > 0  # markup is operator-set; this test pins the pre-markup cost


def test_pricing_cached_input_discount():
    from app.core.config import settings
    m = settings.credit_markup
    # mistral-large-latest: 0.50 in / 0.05 cached_input / 1.50 out (10% policy)
    credits = cost_credits("mistral-large-latest", 1_000_000, 1_000_000,
                           cached_input_tokens=600_000)
    assert credits == pytest.approx((0.4 * 0.50 + 0.6 * 0.05 + 1.50) * m)
    # a reported cached count can never exceed the input count (defensive clamp)
    clamped = cost_credits("mistral-large-latest", 1_000_000, 0,
                           cached_input_tokens=5_000_000)
    assert clamped == pytest.approx(0.05 * m)
    # endpoint override: (input, output, cached) triple discounts...
    ep = cost_credits("off-table-model", 1_000_000, 0,
                      price=(1.25, 4.25, 0.15), cached_input_tokens=1_000_000)
    assert ep == pytest.approx(0.15 * m)
    # ...while a cached-less override (or a table row without cached_input)
    # bills every input token at the full rate - no silent discounts
    flat = cost_credits("off-table-model", 1_000_000, 0,
                        price=(1.25, 4.25), cached_input_tokens=1_000_000)
    assert flat == pytest.approx(1.25 * m)


def test_altcha_challenge_shape():
    ch = create_challenge()
    assert ch["algorithm"] == "SHA-256"
    assert len(ch["challenge"]) == 64
    assert "expires=" in ch["salt"]
    assert ch["maxnumber"] > 0


def test_naming_bootstrap_and_subdomain():
    from app.services import naming

    name = naming.name_from_description(
        "a CRM for freight fleet operators, with booking and dispatch modules")
    assert name == "A CRM for freight fleet operators with booking"
    assert len(name) <= 60
    # empty-ish descriptions still yield a usable name
    assert naming.name_from_description("...") == "Untitled project"
    sub = naming.subdomain_for("a1b2c3d4-0000-0000-0000-000000000000", "Fleet Booking MVP!")
    assert sub == "a1b2c3d4-fleet-booking-mvp"
    # slug part stays capped at 20 chars
    long = naming.subdomain_for("a1b2c3d4-x", "A very long project name that keeps going")
    assert len(long) <= len("a1b2c3d4") + 1 + 20


def test_deposit_pause_kind_mapping():
    from app.services import app_settings

    none_paused = {app_settings.PAUSE_AI: False, app_settings.PAUSE_DIRECT: False}
    assert not app_settings.is_kind_paused(none_paused, "ai")
    assert not app_settings.is_kind_paused(none_paused, "direct_quote")

    ai_paused = {app_settings.PAUSE_AI: True, app_settings.PAUSE_DIRECT: False}
    assert app_settings.is_kind_paused(ai_paused, "ai")
    # pausing AI must not block direct-quote deposits
    assert not app_settings.is_kind_paused(ai_paused, "direct_quote")

    direct_paused = {app_settings.PAUSE_AI: False, app_settings.PAUSE_DIRECT: True}
    assert app_settings.is_kind_paused(direct_paused, "direct_quote")
    assert not app_settings.is_kind_paused(direct_paused, "ai")

    auto_paused = {app_settings.PAUSE_AUTO_DEV: True}
    assert app_settings.is_kind_paused(auto_paused, "auto_dev")
    # each kind's pause is independent of the others
    assert not app_settings.is_kind_paused(auto_paused, "ai")
    assert not app_settings.is_kind_paused(direct_paused, "auto_dev")

    # unknown kinds are never considered paused
    assert not app_settings.is_kind_paused(ai_paused, "something_else")


class _FakeResp:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=httpx.Request("POST", "http://x"),
                response=self)


def _patch_transport(monkeypatch, responses):
    """Feed llm._post_chat a scripted list of _FakeResp (or Exception) and a
    no-op sleep that records the requested delays."""
    from app.services import llm
    seq = iter(responses)
    slept: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda d: slept.append(d))

    def fake_post(url, **kw):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    return slept


def test_llm_retries_on_429_then_succeeds(monkeypatch):
    from app.services import llm

    monkeypatch.setattr(llm.settings, "llm_max_retries", 4)
    slept = _patch_transport(monkeypatch, [
        _FakeResp(429, {"Retry-After": "2"}),
        _FakeResp(503),
        _FakeResp(200),
    ])
    resp = llm._post_chat("http://x/chat/completions", {}, "k")
    assert resp.status_code == 200
    # honored Retry-After (2s) then exponential backoff (2s for attempt index 1)
    assert slept == [2.0, 2.0]


def test_llm_raises_after_retries_exhausted(monkeypatch):
    from app.services import llm

    monkeypatch.setattr(llm.settings, "llm_max_retries", 2)
    slept = _patch_transport(monkeypatch, [_FakeResp(429) for _ in range(3)])
    with pytest.raises(llm.LLMUnavailable):
        llm._post_chat("http://x/chat/completions", {}, "k")
    assert len(slept) == 2  # 2 retries between 3 attempts


def test_llm_does_not_retry_client_error(monkeypatch):
    from app.services import llm

    monkeypatch.setattr(llm.settings, "llm_max_retries", 4)
    slept = _patch_transport(monkeypatch, [_FakeResp(400)])
    with pytest.raises(llm.LLMUnavailable):
        llm._post_chat("http://x/chat/completions", {}, "k")
    assert slept == []  # a 400 is a real error - no retry, no sleep


def test_classify_chat_intent_normalizes(monkeypatch):
    from app.agents import pipeline

    monkeypatch.setattr(pipeline, "record_usage", lambda *a, **k: 0.0)
    usage = {"model": "m", "input_tokens": 1, "output_tokens": 1}

    def scripted(result):
        monkeypatch.setattr(pipeline, "chat_json", lambda *a, **k: (result, usage))

    # a well-formed new_request passes through; request_type is lower-cased
    scripted({"intent": "new_request", "request_type": "Feature", "summary": "Add CSV export"})
    assert pipeline.classify_chat_intent(None, None, "ctx", "add csv export") == {
        "intent": "new_request", "request_type": "feature", "summary": "Add CSV export",
        "question": None, "options": []}

    # an unknown intent and an unknown request_type are scrubbed
    scripted({"intent": "deploy", "request_type": "chore"})
    out = pipeline.classify_chat_intent(None, None, "ctx", "x")
    assert out["intent"] == "none" and out["request_type"] is None

    # resume carries no request_type / summary
    scripted({"intent": "resume"})
    assert pipeline.classify_chat_intent(None, None, "ctx", "you can continue") == {
        "intent": "resume", "request_type": None, "summary": None,
        "question": None, "options": []}

    # a non-dict reply degrades to none
    scripted(["not", "a", "dict"])
    assert pipeline.classify_chat_intent(None, None, "c", "m")["intent"] == "none"


def test_classify_chat_intent_clarify_scrubbing(monkeypatch):
    from app.agents import pipeline

    monkeypatch.setattr(pipeline, "record_usage", lambda *a, **k: 0.0)
    usage = {"model": "m", "input_tokens": 1, "output_tokens": 1}

    def scripted(result):
        monkeypatch.setattr(pipeline, "chat_json", lambda *a, **k: (result, usage))

    # a well-formed clarify passes; string options are accepted, dupes and
    # unusable entries dropped, capped at CLARIFY_MAX_OPTIONS
    scripted({"intent": "clarify", "question": "Which dashboard?",
              "options": [{"label": "Admin", "description": "internal"},
                          "Customer", {"label": "admin"}, 42, {"label": ""},
                          "Both", "Neither", "Extra"]})
    out = pipeline.classify_chat_intent(None, None, "ctx", "add export")
    assert out["intent"] == "clarify" and out["question"] == "Which dashboard?"
    assert [o["label"] for o in out["options"]] == ["Admin", "Customer", "Both", "Neither"]
    assert out["options"][0]["description"] == "internal"
    assert out["options"][1]["description"] is None

    # fewer than 2 usable options -> not a renderable choice -> none
    scripted({"intent": "clarify", "question": "Which?", "options": ["Only one"]})
    assert pipeline.classify_chat_intent(None, None, "c", "m")["intent"] == "none"

    # a clarify with no question -> none
    scripted({"intent": "clarify", "options": ["A", "B"]})
    assert pipeline.classify_chat_intent(None, None, "c", "m")["intent"] == "none"

    # question/options never leak onto other intents
    scripted({"intent": "resume", "question": "Which?", "options": ["A", "B"]})
    out = pipeline.classify_chat_intent(None, None, "c", "m")
    assert out["intent"] == "resume" and out["question"] is None and out["options"] == []


def test_classify_chat_intent_failsafe_on_llm_error(monkeypatch):
    from types import SimpleNamespace

    from app.agents import pipeline
    from app.services.llm import LLMUnavailable

    def boom(*a, **k):
        raise LLMUnavailable("endpoint down")

    monkeypatch.setattr(pipeline, "chat_json", boom)
    # an LLM outage must never trigger a side effect - it classifies as none, and
    # says WHY, so the caller can refuse to have the answering model narrate it
    assert pipeline.classify_chat_intent(None, SimpleNamespace(id="p1"), "c", "m") == {
        "intent": "none", "unavailable": True}


def test_dev_resume_capability_blocked_on_closed_projects():
    from app.api.serializers import dev_resume_capability
    from app.models import Project

    def project(**kw):
        base = dict(kind="ai", status="awaiting_customer", dev_run_state="failed",
                    gitlab_project_id=1, block_auto_development=False)
        base.update(kw)
        return Project(**base)

    # a failed run on an open project is resumable
    enabled, blocker = dev_resume_capability(project())
    assert enabled and blocker is None
    # a closed project never is, whatever the dev sub-state (a finished
    # project takes new work via requests, not by resuming the last run)
    for status in ("finished", "canceled"):
        enabled, blocker = dev_resume_capability(project(status=status))
        assert not enabled and blocker
    enabled, _ = dev_resume_capability(project(status="development"))
    assert enabled


def test_api_token_prefix_follows_settings(monkeypatch):
    # The prefix is cosmetic branding on the plaintext: validation hashes the
    # whole string, so a prefix change never invalidates existing tokens.
    from app.core import security
    from app.core.config import settings
    tok, h = security.new_api_token()
    assert tok.startswith(settings.token_prefix)
    assert security.hash_api_token(tok) == h
    monkeypatch.setattr(settings, "token_prefix", "xx_")
    tok2, _ = security.new_api_token()
    assert tok2.startswith("xx_")
