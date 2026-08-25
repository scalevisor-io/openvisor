"""Account settings: the signed-in user edits their account type, names, tax number
and billing address (§18 billing details - what the invoice is addressed to, and
what Stripe Tax computes the rate from)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.serializers import org_out, user_out
from app.core.db import get_db
from app.core.deps import get_current_user
from app.models import Organization, User
from app.schemas.schemas import AccountUpdateIn
from app.services import countries

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("/countries")
async def billing_countries():
    """The countries an account may be billed from, with their subdivisions and
    what a tax number entered there is called.

    Served rather than restated in the SPA, so the dropdown and the validator
    cannot disagree: a country the form offers and the API refuses is a dead end
    the customer cannot see the shape of.
    """
    return {"countries": countries.out()}


@router.patch("")
async def update_account(body: AccountUpdateIn,
                         user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    """The account page sends the whole form. An organization account requires the
    company details and the full billing address (invoicing needs them); an
    individual may fill the same address in and is not made to, since a personal
    account can be perfectly ordinary without one. Switching type keeps whatever
    was stored so a round-trip loses nothing."""

    def clean(value: str | None) -> str | None:
        return (value or "").strip() or None

    country = (clean(body.country) or "").upper()
    if country and not countries.is_supported(country):
        raise HTTPException(400, f"We cannot bill an account in '{country}' yet")

    # A subdivision is checked against the country being SAVED, not the one on
    # file: a customer moving from Toronto to Berlin sends both in one request,
    # and validating ON against Germany would refuse a legitimate change.
    subdivisions = countries.subdivisions_for(country)
    province = clean(body.province)
    if subdivisions and province and province.upper() not in subdivisions:
        raise HTTPException(400, f"'{province}' is not a state or province of "
                                 f"{countries.SUPPORTED[country].name}")

    if body.account_type == "organization":
        missing = [label for label, value in (
            ("company name", body.company_name),
            ("address", body.address_line1),
            ("postal code", body.postal_code),
            ("city", body.city),
            ("country", body.country),
        ) if clean(value) is None]
        if subdivisions and not province:
            missing.append("state or province")
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
    org.country = country or None
    # A country whose rate is national has no subdivision to hold. Cleared
    # rather than refused: the stale value comes from the form the customer just
    # moved away from, not from anything they typed, and a leftover "ON" on a
    # German address is a subdivision Stripe Tax cannot place.
    org.province = (province.upper() if province else None) if subdivisions else None
    # the org display name follows the account type: company for organizations,
    # the person for individuals
    org.name = (org.company_name if body.account_type == "organization"
                else user.full_name)
    await db.commit()
    return {"user": user_out(user), "org": org_out(org)}
