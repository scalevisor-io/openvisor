from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.serializers import project_out
from app.core.db import get_db
from app.core.deps import get_project_for_user
from app.models import Project
from app.services import project_actions

router = APIRouter(prefix="/api/projects/{project_id}/demo", tags=["demo"])


@router.post("/start")
async def start_demo(project: Project = Depends(get_project_for_user),
                     db: AsyncSession = Depends(get_db)):
    try:
        project_actions.start_demo(project)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    await db.refresh(project, ["repos"])
    return project_out(project)


@router.post("/stop")
async def stop_demo(project: Project = Depends(get_project_for_user),
                    db: AsyncSession = Depends(get_db)):
    try:
        project_actions.stop_demo(project)
    except project_actions.ActionError as exc:
        raise HTTPException(exc.status, exc.detail)
    await db.refresh(project, ["repos"])
    return project_out(project)
