from __future__ import annotations

import os
from fastapi import APIRouter, Cookie, HTTPException
from jose import JWTError, jwt

JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION_USE_256BIT_KEY")
JWT_ALGO = "HS256"

router = APIRouter(prefix="/session", tags=["Session"])

@router.get("/me")
def session_me(handoff_session: str | None = Cookie(default=None)):
    if not handoff_session:
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        data = jwt.decode(handoff_session, JWT_SECRET, algorithms=[JWT_ALGO])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid session")

    if data.get("typ") != "site_session":
        raise HTTPException(status_code=401, detail="Invalid session type")

    return {
        "recipient_id": data.get("recipient_id"),
        "service_url": data.get("service_url"),
        "rid": data.get("rid"),
        "exp": data.get("exp"),
    }