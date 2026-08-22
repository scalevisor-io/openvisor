from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.services import app_settings
from app.services.pricing import load_static

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("/specialities")
async def specialities():
    data = load_static("specialities.json")
    return [s for s in data["specialities"] if s.get("enabled")]


@router.get("/questions")
async def questions():
    return load_static("initial-user-questions.json")


@router.get("/memory-placeholders")
async def memory_placeholders():
    """Conventional Memory keys (with descriptions) the customer can pre-fill."""
    return load_static("memory-placeholders.json")["placeholders"]


@router.get("/legal")
async def legal(response: Response, db: AsyncSession = Depends(get_db)):
    """§legal identity: the operating company's legal name and registered address,
    as set on the admin Settings page. Empty strings mean the admin set nothing and
    the reader should keep its own built-in value.

    The landing is a STATIC build, so its Privacy policy and Terms of service read
    this at runtime from the browser - hence the CORS header, the only one in the
    API. It is safe here and nowhere else: the payload is the same public identity
    the legal pages print, the route is a credential-less GET (a wildcard origin
    forbids cookies by spec), and it is scoped to this route rather than installed
    as middleware over the whole API."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    # Short: an admin who fixes the company name wants the legal pages to
    # follow within the minute, and these two pages are low-traffic.
    response.headers["Cache-Control"] = "public, max-age=60"
    return await app_settings.get_legal_identity(db)


@router.get("/config")
async def config(db: AsyncSession = Depends(get_db)):
    return {
        "deploy_env": settings.deploy_env,
        "deploy_domain": settings.deploy_domain,
        "landing_base_url": settings.landing_base_url,
        "credit_currency": settings.credit_currency,
        "demo_timeout_minutes": settings.demo_timeout_minutes,
        "review_request_credits": settings.review_request_credits,
        "chat_upfront_credits": settings.chat_upfront_credits,
        # shown as the repo-path prefix in the admin program form (§28); not a
        # secret - customers already see per-project gitlab_web_url values
        "gitlab_url": settings.gitlab_url,
        "alpha": True,
        # runtime pause flags (admin-editable) so the SPA can gray out the
        # matching deposit option in the new-project wizard.
        **await app_settings.get_deposit_pause(db),
    }
