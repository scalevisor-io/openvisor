"""§anthropic caching: the OpenHands driver must reach Anthropic natively.

Anthropic serves an OpenAI-compatible surface, so the generic `openai/` route
SUCCEEDS against it - nothing errors, nothing warns, and the cache_control
breakpoints the SDK emits are silently dropped because they are not part of an
OpenAI-shaped request. Measured before this was fixed: a Sonnet 5 build reached
595k input tokens at a 0.00% cache hit rate. After: 81% of input served from
cache on the same workload. At Anthropic's 0.1x cache-read rate that gap is most
of the bill, and it fails in the direction that costs money while looking fine.

Source-level, like test_sandbox_git_preflight: the runner ships as its own image.
"""
from pathlib import Path

import pytest

RUN_DEV = Path("/app/runner_src/run_dev.py")


def _src() -> str:
    if not RUN_DEV.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    return RUN_DEV.read_text()


def test_anthropic_routes_through_the_native_provider():
    src = _src()
    assert 'return f"anthropic/{model}"' in src
    assert 'ANTHROPIC_HOST = "api.anthropic.com"' in src


def test_everything_else_keeps_the_openai_provider():
    """The openai route is what honours base_url for arbitrary gateways; only
    Anthropic is special-cased."""
    src = _src()
    assert 'return f"openai/{model}"' in src


def test_the_platform_base_url_is_dropped_for_the_native_route():
    """The platform stores an OpenAI-shaped base URL ending in /v1 because that is
    what every other provider needs. LiteLLM's anthropic provider owns its own
    endpoint and appends its own version segment, so forwarding it asks for
    /v1/v1/messages - the same trap the claude_sdk driver hits."""
    src = _src()
    i = src.index("if _is_anthropic(_base_url):")
    assert 'llm_kwargs["base_url"] = None' in src[i:i + 600]


def test_current_claude_models_are_registered_as_cache_capable():
    """The pinned SDK's capability table predates these ids, so without this the
    SDK emits no cache breakpoints for them at all."""
    src = _src()
    i = src.index("_CACHE_CAPABLE_ADDITIONS")
    block = src[i:i + 200]
    for model in ("claude-sonnet-5", "claude-opus-5", "claude-fable-5"):
        assert model in block, model
    # registration has to run before the LLM is constructed, or the features are
    # read from the unpatched table
    assert src.index("_register_cache_capable_models()") < src.index("llm_kwargs = dict(")


def test_registration_is_best_effort():
    """An SDK bump that renames the table must degrade to an uncached run, never
    kill the build."""
    src = _src()
    i = src.index("def _register_cache_capable_models")
    body = src[i:i + 1200]
    assert "except Exception" in body
