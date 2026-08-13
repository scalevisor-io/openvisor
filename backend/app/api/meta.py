from fastapi import APIRouter, Depends
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
