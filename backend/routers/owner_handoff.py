import secrets
import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from backend.routers.auth import get_current_user
from backend.models.database import User

# reuse your existing handoff store
from backend.routers.sharing import COOKIE_HANDOFF_STORE as HANDOFF_SESSIONS

router = APIRouter(prefix="/sharing/owner-handoff", tags=["Owner Handoff"])

class OwnerCaptureIn(BaseModel):
    request_id: str
    cookies: list
    localStorage: str | None = "{}"
    sessionStorage: str | None = "{}"
    current_url: str | None = None
    service_url: str | None = None

@router.post("/from-capture")
def create_handoff_from_owner_capture(
    payload: OwnerCaptureIn,
    current_user: User = Depends(get_current_user),
):
    if not payload.request_id.strip():
        raise HTTPException(status_code=400, detail="request_id required")

    handoff_session_id = secrets.token_urlsafe(24)
    HANDOFF_SESSIONS[handoff_session_id] = {
        "cookies": payload.cookies or [],
        "localStorage": payload.localStorage or "{}",
        "sessionStorage": payload.sessionStorage or "{}",
        "current_url": payload.current_url or payload.service_url,
        "service_url": payload.service_url or payload.current_url,
        "created_at": time.time(),
        "expires_at": time.time() + 600,
        "owner_id": current_user.id,
        "request_id": payload.request_id,
    }

    return {
        "request_id": payload.request_id,
        "handoff_session_id": handoff_session_id,
        "handoff_url": f"/sharing/handoff/{handoff_session_id}",
    }