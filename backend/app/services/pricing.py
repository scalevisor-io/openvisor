"""Token → credits conversion from static_data/per_model_price_table.json.
Billing key is `api_model` (the usage-reported model string). Unknown model
strings FAIL LOUD - never bill 0."""
import json
from functools import lru_cache
from pathlib import Path

from app.core.config import settings

STATIC_DIR = Path(__file__).resolve().parent.parent / "static_data"


class UnknownModelError(Exception):
    pass


@lru_cache
def _price_index() -> dict[str, dict]:
    table = json.loads((STATIC_DIR / "per_model_price_table.json").read_text())
    index: dict[str, dict] = {}
    for row in table["models"]:
        aliases = row.get("api_model")
        if aliases is None:
            continue
        if isinstance(aliases, str):
            aliases = [aliases]
        for alias in aliases:
            index[alias] = row
    return index


def cost_credits(api_model: str, input_tokens: int, output_tokens: int,
                 markup: float | None = None,
                 price: tuple | None = None,
                 cached_input_tokens: int = 0, cache_write_tokens: int = 0,
                 cache_write_1h_tokens: int = 0) -> float:
    """Cost in credits (1 credit = 1 EUR; USD treated at parity for the alpha)
    including the markup. markup=None applies the global CREDIT_MARKUP; a
    per-entity factor (e.g. Program.credit_markup) replaces it, never stacks.
    `price` = an (input, output[, cached_input]) per-1M override used only when the
    model is not in the static table (an admin-supplied ModelEndpoint price); the
    static table always wins over it.

    §18 prompt cache: `input_tokens` is the WHOLE prompt, and the three cache
    counters name disjoint slices of it, each on its own rate.
      - `cached_input_tokens` - READS, at `cached_input` (~0.1x base on Anthropic)
      - `cache_write_tokens` - WRITES at the default/5-minute TTL, at `cache_write`
        (1.25x base on Anthropic)
      - `cache_write_1h_tokens` - WRITES at the 1-hour TTL, at `cache_write_1h`
        (2x base on Anthropic)
    Whatever is left over is fresh input at the base rate. A missing rate falls
    back UP the chain (1h -> 5m -> base), so a model whose row carries no write
    rate bills exactly as it did before this existed - an under-bill, never an
    over-bill, and never a crash.

    Writes were folded into plain input until 2026-08-30, which under-billed every
    cache-heavy run: the first production Claude build wrote 32,821 tokens at the
    1-hour rate and was billed for them at 1x instead of 2x."""
    row = _price_index().get(api_model)
    if row is not None:
        in_price, out_price = row["input"], row.get("output", 0)
        cached_price = row.get("cached_input")
        write_price, write_1h_price = row.get("cache_write"), row.get("cache_write_1h")
    elif price is not None:
        in_price, out_price = price[0], price[1]
        cached_price = price[2] if len(price) > 2 else None
        write_price = price[3] if len(price) > 3 else None
        write_1h_price = price[4] if len(price) > 4 else None
    else:
        raise UnknownModelError(f"No price row for model '{api_model}' - refusing to bill 0")
    if cached_price is None:
        cached_price = in_price
    if write_price is None:
        write_price = in_price
    if write_1h_price is None:
        write_1h_price = write_price
    # Clamped in order, each against what the previous slices left, so counters
    # that disagree with input_tokens (a provider reporting them outside the
    # total, a torn snapshot) can never bill more tokens than the prompt had.
    total = max(0, int(input_tokens))
    cached = max(0, min(int(cached_input_tokens or 0), total))
    write = max(0, min(int(cache_write_tokens or 0), total - cached))
    write_1h = max(0, min(int(cache_write_1h_tokens or 0), total - cached - write))
    fresh = total - cached - write - write_1h
    raw = (fresh * in_price + cached * cached_price + write * write_price
           + write_1h * write_1h_price + output_tokens * out_price) / 1_000_000
    return raw * (settings.credit_markup if markup is None else markup)


def is_priced(api_model: str) -> bool:
    """True if the model has a price row - used to fail loud *before* doing
    billable work rather than after (never serve an unbillable call)."""
    return _price_index().get(api_model) is not None


def priced_models() -> list[str]:
    """Every billable api_model alias in the static table."""
    return list(_price_index())


def load_static(name: str) -> dict:
    # Rendered so static catalogs can carry white-label brand placeholders.
    from app.services import brand
    return brand.render_obj(json.loads((STATIC_DIR / name).read_text()))
