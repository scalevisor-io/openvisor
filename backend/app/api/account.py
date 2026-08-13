"""Account settings: the signed-in user edits their account type, names, VAT and address."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.serializers import org_out, user_out
from app.core.db import get_db
from app.core.deps import get_current_user
from app.models import Organization, User
from app.schemas.schemas import AccountUpdateIn

router = APIRouter(prefix="/api/account", tags=["account"])


@router.patch("")
async def update_account(body: AccountUpdateIn,
                         user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    """The account page sends the whole form. An organization account requires the
    company details and the full billing address (invoicing needs them); switching
    back to individual keeps whatever was stored so a round-trip loses nothing."""

    def clean(value: str | None) -> str | None:
        return (value or "").strip() or None

    if body.account_type == "organization":
        missing = [label for label, value in (
            ("company name", body.company_name),
            ("address", body.address_line1),
            ("postal code", body.postal_code),
            ("city", body.city),
            ("country", body.country),
        ) if clean(value) is None]
        if missing:
            raise HTTPException(
                400, f"Required for an organization account: {', '.join(missing)}")
    org = await db.get(Organization, user.org_id)
    user.full_name = body.full_name.strip()
    org.type = body.account_type
    org.company_name = clean(body.company_name)
    org.vat_id = clean(body.vat_id)
    org.address_line1 = clean(body.address_line1)
    org.address_line2 = clean(body.address_line2)
    org.postal_code = clean(body.postal_code)
    org.city = clean(body.city)
    org.country = clean(body.country)
    # the org display name follows the account type: company for organizations,
    # the person for individuals
    org.name = (org.company_name if body.account_type == "organization"
                else user.full_name)
    await db.commit()
    return {"user": user_out(user), "org": org_out(org)}
