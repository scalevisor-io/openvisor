from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, rate_limit
from app.core.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    create_action_token,
    create_session_token,
    hash_password,
    new_csrf_token,
    read_action_token,
    verify_password,
)
from app.api.serializers import org_out, user_out
from app.models import CreditTransaction, Membership, Organization, User, utcnow
from app.schemas.schemas import (
    EmailIn, LoginIn, ResetPasswordIn, SignupIn, TokenIn,
)
from app.services import altcha, brand
from app.workers.celery_app import celery

router = APIRouter(prefix="/api/auth", tags=["auth"])

_secure = not settings.is_local


def _set_session(response: Response, user_id: str) -> None:
    response.set_cookie(SESSION_COOKIE, create_session_token(user_id),
                        max_age=SESSION_MAX_AGE, httponly=True, secure=_secure,
                        samesite="lax", path="/")


def _send_verification(user: User) -> None:
    token = create_action_token("verify", user.id)
    url = f"{settings.app_base_url}/verify-email?token={token}"
    celery.send_task("app.workers.tasks.send_email", args=[
        user.email, brand.subject("Verify your email"),
        f"Welcome to {settings.brand_name}. Verify your email:\n{url}\n\nLink valid 48h."])


@router.get("/csrf")
async def get_csrf(response: Response):
    token = new_csrf_token()
    response.set_cookie(CSRF_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=False,
                        secure=_secure, samesite="lax", path="/")
    return {"csrf_token": token}


@router.get("/altcha")
async def altcha_challenge():
    return altcha.create_challenge()


@router.post("/signup", status_code=201)
async def signup(body: SignupIn, request: Request, db: AsyncSession = Depends(get_db)):
    await rate_limit(request, "signup", 10, 3600)
    if not body.accept_terms:
        raise HTTPException(400, "You must accept the terms of service and privacy policy")
    if not await altcha.verify_payload(body.altcha):
        raise HTTPException(400, "Captcha verification failed")
    existing = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "An account with this email already exists")
    if body.account_type == "organization" and not (
            (body.company_name or "").strip() and (body.full_name or "").strip()):
        raise HTTPException(400, "An organization account needs the company name "
                                 "and your full name")
    org = Organization(
        name=body.company_name or body.email.split("@")[0],
        type=body.account_type, company_name=body.company_name)
    db.add(org)
    await db.flush()
    if settings.signup_credits > 0:
        org.credit_balance = settings.signup_credits
        db.add(CreditTransaction(org_id=org.id, amount=settings.signup_credits,
                                 kind="signup", detail="Welcome credits"))
    user = User(org_id=org.id, email=body.email, password_hash=hash_password(body.password),
                full_name=(body.full_name or "").strip() or None,
                tos_accepted_at=utcnow())
    db.add(user)
    await db.flush()
    db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
    await db.commit()
    _send_verification(user)
    audit.log_action(request, user.email)
    return {"ok": True}


@router.post("/verify-email")
async def verify_email(body: TokenIn, db: AsyncSession = Depends(get_db)):
    uid = read_action_token(body.token, "verify")
    user = await db.get(User, uid) if uid else None
    if user is None:
        raise HTTPException(400, "Invalid or expired token")
    user.email_verified = True
    await db.commit()
    return {"ok": True}


@router.post("/resend-verification")
async def resend_verification(body: EmailIn, request: Request, db: AsyncSession = Depends(get_db)):
    await rate_limit(request, "resend", 5, 3600)
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if user and not user.email_verified:
        _send_verification(user)
    return {"ok": True}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response,
                db: AsyncSession = Depends(get_db)):
    await rate_limit(request, "login", 20, 900)
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    _set_session(response, user.id)
    audit.log_action(request, user.email)
    return {"user": user_out(user)}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/forgot-password")
async def forgot_password(body: EmailIn, request: Request, db: AsyncSession = Depends(get_db)):
    await rate_limit(request, "forgot", 5, 3600)
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if user:
        token = create_action_token("reset", user.id)
        url = f"{settings.app_base_url}/reset-password?token={token}"
        celery.send_task("app.workers.tasks.send_email", args=[
            user.email, brand.subject("Password reset"),
            f"Reset your password:\n{url}\n\nLink valid 48h. Ignore if you didn't ask."])
    return {"ok": True}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordIn, db: AsyncSession = Depends(get_db)):
    uid = read_action_token(body.token, "reset")
    user = await db.get(User, uid) if uid else None
    if user is None:
        raise HTTPException(400, "Invalid or expired token")
    user.password_hash = hash_password(body.password)
    await db.commit()
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    org = await db.get(Organization, user.org_id)
    return {"user": user_out(user), "org": org_out(org)}
