import secrets
import time
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from backend.models.database import get_db, User, Credential
from backend.routers.auth import get_current_user

# Reuse existing handoff store from sharing module
from backend.routers.sharing import COOKIE_HANDOFF_STORE as HANDOFF_SESSIONS  # adapte si nom différent

router = APIRouter(prefix="/sharing/final-capture", tags=["Final Capture Share"])

# in-memory store for this new flow (additive, doesn't touch old flow)
FINAL_CAPTURE_REQUESTS: dict[str, dict] = {}
FINAL_CAPTURE_TOKENS: dict[str, dict] = {}

from urllib.parse import urlparse

class FinishWithHandoffUrlIn(BaseModel):
    request_id: str
    handoff_url: str


@router.post("/finish-by-handoff")
def finish_capture_with_handoff_url(
    payload: FinishWithHandoffUrlIn,
    current_user: User = Depends(get_current_user),
):
    req = FINAL_CAPTURE_REQUESTS.get(payload.request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req["owner_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed")
    if req["status"] != "started":
        raise HTTPException(status_code=400, detail=f"Invalid request status: {req['status']}")

    # extract handoff_session_id from /sharing/handoff/{id}
    path = urlparse(payload.handoff_url).path or payload.handoff_url
    marker = "/sharing/handoff/"
    idx = path.find(marker)
    if idx < 0:
        raise HTTPException(status_code=400, detail="Invalid handoff_url format")
    handoff_session_id = path[idx + len(marker):].strip("/")

    if handoff_session_id not in HANDOFF_SESSIONS:
        raise HTTPException(status_code=404, detail="Handoff session not found/expired")

    final_token = secrets.token_urlsafe(24)
    FINAL_CAPTURE_TOKENS[final_token] = {
        "request_id": req["request_id"],
        "owner_id": req["owner_id"],
        "recipient_id": req["recipient_id"],
        "handoff_session_id": handoff_session_id,
        "created_at": time.time(),
        "expires_at": time.time() + req["ttl_seconds"],
        "max_uses": req["max_uses"],
        "uses": 0,
        "revoked": False,
    }

    req["status"] = "completed"
    req["handoff_session_id"] = handoff_session_id

    return {
        "request_id": req["request_id"],
        "status": "completed",
        "final_token": final_token,
        "expires_at": FINAL_CAPTURE_TOKENS[final_token]["expires_at"],
        "max_uses": FINAL_CAPTURE_TOKENS[final_token]["max_uses"],
    }








class StartRequestIn(BaseModel):
    credential_id: int
    recipient_email: EmailStr
    ttl_seconds: int = 600
    max_uses: int = 1


@router.post("/start")
def start_final_capture_request(
    payload: StartRequestIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = db.query(Credential).filter(Credential.id == payload.credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    if cred.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="This credential does not belong to current owner")
    if not (cred.service_url or "").strip():
        raise HTTPException(status_code=400, detail="Credential missing service_url")

    recipient = db.query(User).filter(User.email.ilike(payload.recipient_email.strip())).first()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient user not found")

    request_id = secrets.token_urlsafe(16)
    FINAL_CAPTURE_REQUESTS[request_id] = {
        "request_id": request_id,
        "owner_id": current_user.id,
        "recipient_id": recipient.id,
        "recipient_email": recipient.email,
        "credential_id": cred.id,
        "service_url": cred.service_url.strip(),
        "created_at": time.time(),
        "status": "started",
        "ttl_seconds": max(60, min(payload.ttl_seconds, 86400)),
        "max_uses": max(1, min(payload.max_uses, 10)),
    }

    return {
        "request_id": request_id,
        "service_url": FINAL_CAPTURE_REQUESTS[request_id]["service_url"],
        "status": "started",
    }


class FinishCaptureIn(BaseModel):
    request_id: str
    cookies: list
    localStorage: str | None = None
    sessionStorage: str | None = None
    current_url: str | None = None


@router.post("/finish")
def finish_capture_and_generate_token(
    payload: FinishCaptureIn,
    current_user: User = Depends(get_current_user),
):
    req = FINAL_CAPTURE_REQUESTS.get(payload.request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req["owner_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed")
    if req["status"] != "started":
        raise HTTPException(status_code=400, detail=f"Invalid request status: {req['status']}")

    handoff_session_id = secrets.token_urlsafe(24)
    HANDOFF_SESSIONS[handoff_session_id] = {
        "cookies": payload.cookies or [],
        "localStorage": payload.localStorage if payload.localStorage else json.dumps({}),
        "sessionStorage": payload.sessionStorage if payload.sessionStorage else json.dumps({}),
        "current_url": payload.current_url or req["service_url"],
        "service_url": req["service_url"],
        "created_at": time.time(),
        "expires_at": time.time() + 600,
        "owner_id": req["owner_id"],
        "recipient_id": req["recipient_id"],
    }

    final_token = secrets.token_urlsafe(24)
    FINAL_CAPTURE_TOKENS[final_token] = {
        "request_id": req["request_id"],
        "owner_id": req["owner_id"],
        "recipient_id": req["recipient_id"],
        "handoff_session_id": handoff_session_id,
        "created_at": time.time(),
        "expires_at": time.time() + req["ttl_seconds"],
        "max_uses": req["max_uses"],
        "uses": 0,
        "revoked": False,
    }

    req["status"] = "completed"
    req["handoff_session_id"] = handoff_session_id

    return {
        "request_id": req["request_id"],
        "status": "completed",
        "final_token": final_token,
        "expires_at": FINAL_CAPTURE_TOKENS[final_token]["expires_at"],
        "max_uses": FINAL_CAPTURE_TOKENS[final_token]["max_uses"],
    }


@router.post("/resolve/{final_token}")
def resolve_final_token(
    final_token: str,
    current_user: User = Depends(get_current_user),
):
    t = FINAL_CAPTURE_TOKENS.get(final_token)
    if not t:
        raise HTTPException(status_code=404, detail="Token not found")
    if t["revoked"]:
        raise HTTPException(status_code=403, detail="Token revoked")
    if time.time() > t["expires_at"]:
        raise HTTPException(status_code=403, detail="Token expired")
    if t["uses"] >= t["max_uses"]:
        raise HTTPException(status_code=403, detail="Usage limit reached")
    if t["recipient_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    hs = t["handoff_session_id"]
    if hs not in HANDOFF_SESSIONS:
        raise HTTPException(status_code=404, detail="Handoff session missing/expired")

    t["uses"] += 1
    return {
        "handoff_url": f"/sharing/handoff/{hs}",
        "remaining_uses": max(0, t["max_uses"] - t["uses"]),
        "expires_at": t["expires_at"],
    }