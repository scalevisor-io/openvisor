"""Where a model's price comes from - the ONE answer, for billing AND display.

A model can be priced in two places: the static table (`pricing.py`, the
platform's own catalog) and an admin-supplied `ModelEndpoint` row (a model the
platform doesn't know yet, priced by whoever added the endpoint). `cost_credits`
is the pure calculator over both - the static table always wins, the endpoint
tuple is its `price=` fallback - so EVERY caller has to resolve the endpoint
half before asking for a number.

Callers used to do that lookup themselves, and the one that forgot (the live
build-console estimate) silently showed no cost for months while the worker
billed the same run correctly. Hence this module: resolution lives here, in a
sync flavour for the workers and an async one for the API.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

Price = tuple[float, float, float | None]


def price_tuple(ep) -> Price:
    """A ModelEndpoint row as the (input, output, cached_input) per-1M tuple
    `pricing.cost_credits(price=)` takes. cached_input None = no prompt-cache
    discount (every input token bills at the full rate)."""
    return (ep.input_price, ep.output_price or 0.0, ep.cached_input_price)


def for_model(db: Session, api_model: str) -> Price | None:
    """The admin-supplied price for ONE model, or None when no endpoint prices
    it (then only the static table can, and an unknown model raises rather than
    billing 0)."""
    from app.models import ModelEndpoint
    ep = (db.query(ModelEndpoint)
          .filter(ModelEndpoint.model_name == api_model,
                  ModelEndpoint.input_price.isnot(None))
          .first())
    return price_tuple(ep) if ep else None


async def all_prices(db: AsyncSession) -> dict[str, Price]:
    """{api_model: price} for every priced endpoint - the async twin, for
    surfaces that price a model they only learn about later (the build console
    reads the model out of the runner's snapshot, in a threadpool with no
    session). The table is admin-sized: one query, no cache to go stale."""
    from app.models import ModelEndpoint
    rows = (await db.execute(select(ModelEndpoint).where(
        ModelEndpoint.input_price.isnot(None)))).scalars().all()
    return {ep.model_name: price_tuple(ep) for ep in rows}
