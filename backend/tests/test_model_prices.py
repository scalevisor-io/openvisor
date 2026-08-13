"""§pricing: a model's price has TWO sources - the static table and an admin
ModelEndpoint row - and every surface that puts a number in front of someone
must consult both. The bug this pins: the live build-console estimate consulted
only the static table, so a project on an endpoint-priced model showed no cost
for the whole run while the worker billed it correctly.
"""
import pathlib
import re

import pytest
from sqlalchemy import delete

from app.core.db import SyncSession
from app.models import ModelEndpoint
from app.services import llm, model_prices, pricing

BACKEND = pathlib.Path("/app/app")


@pytest.fixture
def endpoint():
    with SyncSession() as db:
        ep = ModelEndpoint(label="Test provider", base_url="https://x.example/v1",
                           model_name="prices-test-model", api_key_enc="x",
                           input_price=2.0, output_price=12.0, cached_input_price=0.2)
        db.add(ep)
        db.commit()
        eid = ep.id
    yield "prices-test-model"
    with SyncSession() as db:
        db.execute(delete(ModelEndpoint).where(ModelEndpoint.id == eid))
        db.commit()


def test_for_model_and_all_prices_agree(endpoint):
    with SyncSession() as db:
        assert model_prices.for_model(db, endpoint) == (2.0, 12.0, 0.2)
        assert model_prices.for_model(db, "no-such-model") is None
        # the billing path reads the same resolution
        assert llm._endpoint_price(db, endpoint) == (2.0, 12.0, 0.2)


def test_endpoint_price_makes_an_unpriced_model_billable(endpoint):
    """Without the endpoint tuple cost_credits refuses to bill; with it the
    number is the endpoint's own rates at the platform markup."""
    with pytest.raises(pricing.UnknownModelError):
        pricing.cost_credits(endpoint, 1_000_000, 0)
    with SyncSession() as db:
        price = model_prices.for_model(db, endpoint)
    assert pricing.cost_credits(endpoint, 1_000_000, 0, markup=1.0, price=price) == 2.0
    # cached reads bill at the cached rate, not the input rate
    assert pricing.cost_credits(endpoint, 1_000_000, 0, markup=1.0, price=price,
                                cached_input_tokens=1_000_000) == pytest.approx(0.2)


def test_every_cost_credits_caller_resolves_the_endpoint_price():
    """A new call site that forgets `price=` silently under-reports (or refuses)
    for endpoint-priced models - exactly the console bug. Any legitimate
    exception must be listed here with its reason."""
    # Only where cost_credits is DEFINED. Everywhere else must pass price=,
    # including surfaces that receive the resolved tuple from their caller -
    # devfeed is not exempt here precisely because that is where this bug lived.
    allowed = {"services/pricing.py"}
    offenders = []
    for path in BACKEND.rglob("*.py"):
        rel = str(path.relative_to(BACKEND))
        if rel in allowed:
            continue
        text = path.read_text()
        for call in re.finditer(r"cost_credits\((.*?)\)", text, re.S):
            args = call.group(1)
            if "price=" not in args:
                offenders.append(f"{rel}: cost_credits({args.strip()[:80]}...)")
    assert not offenders, (
        "these call sites price a model from the static table only:\n  "
        + "\n  ".join(offenders)
        + "\nResolve the endpoint half through services/model_prices first.")
