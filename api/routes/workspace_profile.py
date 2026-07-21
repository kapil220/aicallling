from typing import Annotated
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from api.db import db_client
from api.db.models import UserModel
from api.enums import UserConfigurationKey
from api.services.auth.depends import get_user

router = APIRouter(prefix="/user", tags=["user"])


class WorkspaceProfile(BaseModel):
    company_name: str | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str | None) -> str | None:
        if v is not None and v not in available_timezones():
            raise ValueError("Unknown IANA timezone")
        return v


@router.get("/workspace-profile", response_model=WorkspaceProfile)
async def get_workspace_profile(user: Annotated[UserModel, Depends(get_user)]):
    stored = await db_client.get_user_configuration_value(
        user.id, UserConfigurationKey.WORKSPACE_PROFILE.value
    )
    return WorkspaceProfile(**(stored or {}))


@router.put("/workspace-profile", response_model=WorkspaceProfile)
async def put_workspace_profile(
    body: WorkspaceProfile, user: Annotated[UserModel, Depends(get_user)]
):
    await db_client.upsert_user_configuration_value(
        user.id,
        UserConfigurationKey.WORKSPACE_PROFILE.value,
        body.model_dump(),
    )
    return body
