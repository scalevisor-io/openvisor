"""§spend floor: the routes that spend model tokens before anything is paid for.

Three session-authed endpoints dispatch an LLM call with nothing collected yet -
project evaluation (four calls, and a draft stays a draft so it replays forever),
filing a Request (its LLM title) and the pre-creation Request estimate. Each is
capped per ORG, and the tasks behind them refuse once the wallet is further than
CREDIT_DEBT_LIMIT credits negative: `llm._debit_org` has no lower bound, so
without that floor an account with no payment on file bills indefinitely and the
ledger just records a debt nobody collects.

Also pinned here: `deps.client_ip`, because keying an auth limiter on
`request.client.host` behind a reverse proxy collapses every caller into one
bucket - a platform-wide lockout, not a per-caller cap.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from app.core import deps
from app.core.config import settings
from app.core.db import SyncSession
from app.core.security import hash_password
from app.main import app
from app.models import Organization, Project, User
from app.services import llm


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _req(headers: dict, client_host: str = "10.0.0.1") -> StarletteRequest:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return StarletteRequest({"type": "http", "headers": raw, "client": (client_host, 1234)})


def test_client_ip_prefers_the_edge_over_the_reverse_proxy():
    # Nothing forwarded: the peer is all we have.
    assert deps.client_ip(_req({})) == "10.0.0.1"
    # Cloudflare rewrites this on every request that crosses the edge.
    assert deps.client_ip(_req({"CF-Connecting-IP": "203.0.113.9"})) == "203.0.113.9"
    # Without Cloudflare, the first XFF hop is the caller; the proxy appends itself.
    assert deps.client_ip(_req({"X-Forwarded-For": "203.0.113.9, 172.16.0.2"})) == "203.0.113.9"
    # CF wins over XFF - only the edge's own header is rewritten per request.
    assert deps.client_ip(_req({"CF-Connecting-IP": "203.0.113.9",
                                "X-Forwarded-For": "198.51.100.4"})) == "203.0.113.9"


def test_auth_limiters_are_never_keyed_on_the_proxy():
    """A regression guard, not a behaviour test: `rate_limit` falls back to
    `request.client.host`, which is Traefik for every caller. Any auth route that
    takes the fallback turns its cap into a platform-wide outage the moment one
    caller spends it."""
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "api" / "auth.py").read_text()
    calls = re.findall(r"await rate_limit\((.*?)\)\n", src, re.S)
    # Every call must be accounted for - an under-matching regex would pass here
    # while leaving a proxy-keyed limiter in place.
    assert len(calls) == src.count("await rate_limit("), "regex missed a call site"
    assert calls, "auth.py should still rate-limit its public routes"
    for call in calls:
        assert "identity=" in call, f"auth rate_limit without an identity: {call}"


def _org_project(balance: float):
    with SyncSession() as db:
        org = Organization(name=f"floor-{uuid.uuid4().hex[:8]}", type="individual",
                           credit_balance=balance)
        db.add(org)
        db.flush()
        project = Project(org_id=org.id, name="Floor", kind="ai", speciality="general-webapp",
                          description="x", status="draft")
        db.add(project)
        db.commit()
        return org.id, project.id


def test_spend_allowed_tracks_the_debt_limit():
    org_id, _ = _org_project(0.0)
    # An empty wallet still evaluates - a customer has to be able to price the
    # work before paying for it. Debt past the limit is where it stops.
    with SyncSession() as db:
        assert llm.spend_allowed(db, org_id) is True

    org_id, _ = _org_project(-(settings.credit_debt_limit + 1))
    with SyncSession() as db:
        assert llm.spend_allowed(db, org_id) is False

    org_id, _ = _org_project(-(settings.credit_debt_limit - 1))
    with SyncSession() as db:
        assert llm.spend_allowed(db, org_id) is True

    with SyncSession() as db:
        assert llm.spend_allowed(db, "no-such-org") is False


def test_evaluation_refuses_visibly_past_the_debt_limit():
    """A refusal the customer can see and act on - a silently pending evaluation
    that never resolves is the failure mode this replaces."""
    from app.workers import tasks

    _, pid = _org_project(-(settings.credit_debt_limit + 5))
    tasks.evaluate_project(pid)
    with SyncSession() as db:
        ev = db.get(Project, pid).evaluation
    assert ev["state"] == "failed"
    assert "credits" in ev["error"].lower()


def test_request_estimate_refuses_past_the_debt_limit():
    from app.workers import tasks

    _, pid = _org_project(-(settings.credit_debt_limit + 5))
    out = tasks.estimate_request(pid, {"type": "feature", "body": "add a button"})
    assert out == {"project_id": pid, "available": False, "reason": "insufficient_credits"}


def test_request_estimate_never_averages_another_org_s_builds():
    """The estimate and its explanation are shown to the customer, so anchoring
    it on platform-wide dev-run costs told every customer what every other
    customer's builds cost and how long they took."""
    from app.models import CreditTransaction
    from app.workers import tasks

    other_org, other_pid = _org_project(500.0)
    with SyncSession() as db:
        db.add(CreditTransaction(org_id=other_org, project_id=other_pid, amount=-42.0,
                                 kind="consumption", detail="dev run", tokens=1000))
        db.commit()

    _, pid = _org_project(500.0)  # funded, but no dev runs of its own
    out = tasks.estimate_request(pid, {"type": "feature", "body": "add a button"})
    assert out == {"project_id": pid, "available": False, "reason": "no_history"}
