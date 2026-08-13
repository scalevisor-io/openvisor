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
                 price: tuple[float, float] | tuple[float, float, float | None] | None = None,
                 cached_input_tokens: int = 0) -> float:
    """Cost in credits (1 credit = 1 EUR; USD treated at parity for the alpha)
    including the markup. markup=None applies the global CREDIT_MARKUP; a
    per-entity factor (e.g. Program.credit_markup) replaces it, never stacks.
    `price` = an (input, output[, cached_input]) per-1M override used only when the
    model is not in the static table (an admin-supplied ModelEndpoint price); the
    static table always wins over it. `cached_input_tokens` = the provider-reported
    prompt-cache READS (a subset of input_tokens), billed at the row's
    `cached_input` price - absent price means no discount, so a model without a
    verified cached rate keeps billing every input token at the full rate."""
    row = _price_index().get(api_model)
    if row is not None:
        in_price, out_price = row["input"], row.get("output", 0)
        cached_price = row.get("cached_input")
    elif price is not None:
        in_price, out_price = price[0], price[1]
        cached_price = price[2] if len(price) > 2 else None
    else:
        raise UnknownModelError(f"No price row for model '{api_model}' - refusing to bill 0")
    if cached_price is None:
        cached_price = in_price
    cached = max(0, min(int(cached_input_tokens or 0), int(input_tokens)))
    raw = ((input_tokens - cached) * in_price + cached * cached_price
           + output_tokens * out_price) / 1_000_000
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
