from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


from pydantic import BaseModel

class AssistedCompleteRequest(BaseModel):
    proof: str

class AssistedCreateRequest(BaseModel):
    token: str = Field(..., description="Share token/session token shown to recipient")


class AssistedRequestOut(BaseModel):
    request_id: int
    status: str
    service_url: str
    expires_at: float


class AssistedApproveOut(BaseModel):
    request_id: int
    status: str
    delegation_token: str
    handoff_url: str
    expires_at: float


class AssistedStatusOut(BaseModel):
    request_id: int
    status: str
    handoff_url: Optional[str] = None
    expires_at: float