from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import new_api_token
from app.models import ApiToken, User
from app.schemas.schemas import ApiTokenIn

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


@router.get("")
async def list_tokens(user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ApiToken).where(ApiToken.user_id == user.id)
                             .order_by(ApiToken.created_at.desc()))).scalars().all()
    return [{"id": t.id, "name": t.name, "scope": t.scope, "created_at": t.created_at,
             "last_used_at": t.last_used_at} for t in rows]


@router.post("", status_code=201)
async def create_token(body: ApiTokenIn, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    plaintext, token_hash = new_api_token()
    row = ApiToken(user_id=user.id, token_hash=token_hash, name=body.name, scope="user")
    db.add(row)
    await db.commit()
    return {"id": row.id, "name": row.name, "token": plaintext}


@router.delete("/{token_id}")
async def delete_token(token_id: str, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    row = await db.get(ApiToken, token_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Not found")
    await db.delete(row)
    await db.commit()
    return {"ok": True}
