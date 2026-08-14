"""Public white-label settings: brand identity + activity catalog for the
frontends, plus intake availability for the hub catalog (§hub pass-through
capability discovery). Unauthenticated by design (the SPA fetches it before
auth resolves, like /api/meta/config); nothing here is sensitive."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.services import app_settings, routines as routines_svc, speciality as speciality_svc
from app.services.pricing import load_static

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def public_settings(db: AsyncSession = Depends(get_db)):
    specialities = load_static("specialities.json")["specialities"]
    flags = await app_settings.get_deposit_pause(db)
    fee_overrides = await app_settings.get_value(db, speciality_svc.FEE_OVERRIDES_KEY)
    return {
        "brand_name": settings.brand_name,
        "brand_slug": settings.brand_slug,
        "brand_color_primary": settings.brand_color_primary,
        "brand_color_secondary": settings.brand_color_secondary,
        "consultant_name": settings.consultant_name,
        "consultant_first_name": settings.consultant_first_name,
        "credit_currency": settings.credit_currency,
        # What this instance offers the hub network (development and/or
        # project_management); absent on older spokes, so hub readers fail soft.
        "capabilities": settings.capabilities_list,
        # Rich track metadata feeds hub-side matching/decomposition; hub
        # readers stay fail-soft because older spokes publish only
        # id/label/description.
        "specialities": [
            {
                "id": s["id"],
                "label": s["label"],
                "description": s.get("description", ""),
                "short_label": s.get("short_label", s["label"]),
                "icon": s.get("icon", ""),
                "deliverable_type": s.get("deliverable_type", "deployed_demo"),
                "knowledge_tags": s.get("knowledge_tags", []),
                "capabilities": s.get("capabilities", []),
                "complexity_baseline": s.get("complexity_baseline", "medium"),
                # The track's base engagement fee: charged once with the first
                # funding (it rides the evaluation estimate), then usage is
                # metered per token as always. 0 = no base fee. Admin overrides
                # (per instance) win over the specialities.json default.
                "base_fee_credits": speciality_svc.effective_base_fee(s, fee_overrides),
            }
            for s in specialities
            if s.get("enabled")
        ],
        # Advisory for catalogs/hubs (a stale reader only mis-displays); project
        # creation re-checks the pause flags server-side (never-trust-client).
        "intake": {
            "ai_enabled": not app_settings.is_kind_paused(flags, "ai"),
            "direct_quote_enabled": not app_settings.is_kind_paused(flags, "direct_quote"),
            "auto_dev_enabled": not app_settings.is_kind_paused(flags, "auto_dev"),
            "chat_enabled": not app_settings.is_kind_paused(flags, "chat"),
        },
        # §routines: advisory for the SPA (it hides the tab when off); every
        # routine write re-checks the flag server-side.
        "routines_enabled": not await app_settings.get_flag(
            db, routines_svc.ROUTINES_DISABLED),
    }
