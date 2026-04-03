from __future__ import annotations

import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jose import JWTError, jwt

JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION_USE_256BIT_KEY")
JWT_ALGO = "HS256"

router = APIRouter(tags=["Handoff"])

# One-time store (MVP). In prod, put in DB/Redis.
HANDOFF_CONSUMED: set[str] = set()
HANDOFF_CONSUMED_TTL: dict[str, float] = {}  # token_jti -> expires_at


def _cleanup_consumed(now: float):
    # cleanup old JTIs
    for jti, exp in list(HANDOFF_CONSUMED_TTL.items()):
        if exp <= now:
            HANDOFF_CONSUMED.discard(jti)
            HANDOFF_CONSUMED_TTL.pop(jti, None)


def verify_handoff_token(token: str) -> dict:
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired handoff_token")

    if data.get("typ") != "handoff":
        raise HTTPException(status_code=400, detail="Not a handoff token")

    return data


def create_site_session_cookie(payload: dict, ttl_seconds: int = 3600) -> str:
    """
    Create a site session cookie (signed JWT) for YOUR SITE.
    This is what your frontend will treat as "logged in".
    """
    now = int(time.time())
    session = {
        "typ": "site_session",
        "iat": now,
        "exp": now + ttl_seconds,
        # Copy the delegated identity/scope
        "recipient_id": payload.get("recipient_id"),
        "service_url": payload.get("service_url"),
        "rid": payload.get("rid"),
    }
    return jwt.encode(session, JWT_SECRET, algorithm=JWT_ALGO)


@router.get("/handoff")
def handoff_exchange(
    request: Request,
    handoff_token: str,
    redirect_to: Optional[str] = "/app",
):
    """
    Recipient opens:
      GET /handoff?handoff_token=...&redirect_to=/app

    Server:
      - verifies token
      - consumes one-time jti
      - issues a first-party cookie for YOUR SITE
      - redirects to redirect_to
    """
    data = verify_handoff_token(handoff_token)

    now = time.time()
    _cleanup_consumed(now)

    # Require a jti to prevent replay
    jti = data.get("jti")
    exp = data.get("exp")
    if not jti or not exp:
        raise HTTPException(status_code=400, detail="handoff_token missing jti/exp (replay protection)")

    if jti in HANDOFF_CONSUMED:
        raise HTTPException(status_code=409, detail="handoff_token already used")

    HANDOFF_CONSUMED.add(jti)
    HANDOFF_CONSUMED_TTL[jti] = float(exp)

    # Create a site session cookie
    cookie_value = create_site_session_cookie(data, ttl_seconds=3600)

    resp = RedirectResponse(url=redirect_to, status_code=302)

    # Cookie options: adjust domain if needed
    resp.set_cookie(
        key="handoff_session",
        value=cookie_value,
        httponly=True,
        secure=(request.url.scheme == "https"),
        samesite="lax",
        max_age=3600,
        path="/",
    )
    return resp


@router.get("/app", response_class=HTMLResponse)
def app_home():
    """
    Demo page that shows you're logged in if cookie exists.
    Replace by your real frontend entrypoint.
    """
    return HTMLResponse(
        """
        <html>
          <body>
            <h2>Handoff App</h2>
            <p>If you reached here via /handoff, a cookie named <b>handoff_session</b> was set.</p>
            <p>Your frontend can now read session on the server side (or call /session/me).</p>
          </body>
        </html>
        """
    )
