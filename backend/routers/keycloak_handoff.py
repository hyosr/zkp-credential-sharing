import asyncio
import hashlib
import json
import secrets
import time
import urllib.parse
from threading import Lock
from typing import Any, Dict, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.models.database import SharedAccess, Credential, User, get_db
from backend.routers.auth import get_current_user
from backend.crypto.encryption import ShareEncryptor
from backend.relay.playwright_relay import login_and_get_cookies

# Reuse your existing relay profiles if you want
from backend.routers.sharing import RELAY_PROFILES

# Reuse your existing Keycloak device flow helpers if you have them:
# If your repo already has backend/integrations/keycloak_device_flow.py, import from there instead.
from backend.integrations.keycloak_device_flow import start_device_flow, poll_device_flow_token

from backend.auth.keycloak_auth import get_keycloak_user

router = APIRouter(prefix="/keycloak-sharing/handoff", tags=["Keycloak Passwordless Handoff"])

# ---- In-memory KC sessions (TTL short) ----
KC_SESSION_STORE: dict[str, dict] = {}
KC_SESSION_LOCK = Lock()
KC_SESSION_TTL_SECONDS = 600  # 10 minutes

def _kc_cleanup(now: float):
    for sid, v in list(KC_SESSION_STORE.items()):
        if now - v.get("created_at", now) > KC_SESSION_TTL_SECONDS:
            KC_SESSION_STORE.pop(sid, None)

def _kc_put(data: dict) -> str:
    now = time.time()
    sid = secrets.token_urlsafe(24)
    with KC_SESSION_LOCK:
        _kc_cleanup(now)
        data["created_at"] = now
        KC_SESSION_STORE[sid] = data
    return sid

def _kc_get(sid: str) -> dict | None:
    now = time.time()
    with KC_SESSION_LOCK:
        v = KC_SESSION_STORE.get(sid)
        if not v:
            return None
        if now - v.get("created_at", now) > KC_SESSION_TTL_SECONDS:
            KC_SESSION_STORE.pop(sid, None)
            return None
        return v

def _kc_consume(sid: str) -> dict | None:
    now = time.time()
    with KC_SESSION_LOCK:
        v = _kc_get(sid)
        if not v:
            return None
        return KC_SESSION_STORE.pop(sid, None)


@router.post("/start")
def start_handoff(
    share_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Owner (A) starts a Keycloak passwordless handoff for a given share_id.
    Returns device flow information + a kc_session_id to share with recipient (B).
    """
    shared = db.query(SharedAccess).filter(SharedAccess.id == share_id).first()
    if not shared:
        raise HTTPException(status_code=404, detail="Share not found")

    if shared.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not owner of this share")

    if shared.is_revoked:
        raise HTTPException(status_code=403, detail="Share revoked")

    if time.time() > (shared.expires_at or 0):
        raise HTTPException(status_code=403, detail="Share expired")

    # Start Keycloak Device Flow (recipient will authenticate on Keycloak UI)
    df = start_device_flow()
    if "device_code" not in df:
        raise HTTPException(status_code=400, detail=df.get("detail", df))

    kc_session_id = _kc_put({
        "share_id": share_id,
        "owner_id": current_user.id,
        "device_code": df["device_code"],
        "interval": int(df.get("interval", 5)),
    })

    # Handoff link recipient opens (your extension or lightweight client can handle it)
    # Example: http://localhost:8001/keycloak-sharing/handoff/complete/<kc_session_id>
    base = str(request.base_url).rstrip("/")
    recipient_link = f"{base}/keycloak-sharing/handoff/complete/{kc_session_id}"

    return {
        "kc_session_id": kc_session_id,
        "expires_in": KC_SESSION_TTL_SECONDS,
        "device_flow": {
            "verification_uri": df.get("verification_uri"),
            "verification_uri_complete": df.get("verification_uri_complete"),
            "user_code": df.get("user_code"),
            "expires_in": df.get("expires_in"),
            "interval": df.get("interval"),
        },
        "recipient_link": recipient_link,
        "message": "Share this recipient_link (or QR). Recipient completes Keycloak login then client finalizes handoff.",
    }


@router.get("/complete/{kc_session_id}")
def complete_page(kc_session_id: str):
    """
    Simple landing page. Your extension/light client can read kc_session_id from URL and call /finalize.
    """
    return {
        "kc_session_id": kc_session_id,
        "next": f"/keycloak-sharing/handoff/finalize/{kc_session_id}",
        "message": "Open this link with the browser that has the extension. The extension should finalize automatically.",
    }


@router.post("/finalize/{kc_session_id}")
async def finalize_handoff(
    kc_session_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Recipient-side finalize:
    - Poll Keycloak using stored device_code
    - Verify token via get_keycloak_user (JWKS)
    - If OK => perform relay-login server-side => return handoff session_id (cookies never shown)
    """
    sess = _kc_consume(kc_session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="kc_session not found or expired")

    # Poll Keycloak token
    device_code = sess["device_code"]
    interval = int(sess.get("interval", 5))
    tok = poll_device_flow_token(device_code=device_code, interval=interval)
    if "access_token" not in tok:
        raise HTTPException(status_code=403, detail=tok.get("detail", tok))

    # Verify Keycloak token (JWKS). We call get_keycloak_user logic by faking Request header.
    # Easiest: directly decode/verify in keycloak_auth helper; but to keep structure, do minimal verify here:
    # We'll reuse get_keycloak_user by creating a synthetic request-like wrapper is complex;
    # So instead, we just call the internal verify function if you have it.
    # If your keycloak_auth exposes only dependency, keep a small verify function there.
    #
    # For now: we will require that your keycloak_auth.get_keycloak_user() can be used elsewhere,
    # or you create a verify_keycloak_token(token)->claims in that module.

    from backend.auth.keycloak_auth import verify_keycloak_token  # you should add this helper (see below)
    claims = verify_keycloak_token(tok["access_token"])

    # Enforce recipient authorization (recommended)
    recipient_email = (claims.get("email") or "").strip().lower()
    share_id = int(sess["share_id"])

    shared = db.query(SharedAccess).filter(SharedAccess.id == share_id).first()
    if not shared:
        raise HTTPException(status_code=404, detail="Share not found")

    if shared.is_revoked:
        raise HTTPException(status_code=403, detail="Share revoked")

    if time.time() > (shared.expires_at or 0):
        raise HTTPException(status_code=403, detail="Share expired")

    if recipient_email and (shared.recipient_email or "").strip().lower() != recipient_email:
        raise HTTPException(status_code=403, detail="Recipient email mismatch")

    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    # Decrypt share payload server-side (recipient never sees password)
    try:
        encrypted_data = json.loads(shared.encrypted_payload or "{}")
        plaintext = ShareEncryptor.decrypt_from_share(encrypted_data, shared.share_key) or ""
        payload = json.loads(plaintext) if plaintext.strip().startswith("{") else {"password": plaintext}
        password = (payload.get("password") or "").strip()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot decrypt share payload: {e}")

    if not password:
        raise HTTPException(status_code=400, detail="No password found in share payload")

    service_url = (cred.service_url or "").strip()
    if not service_url:
        raise HTTPException(status_code=400, detail="Credential has no service_url")

    domain = urllib.parse.urlparse(service_url).netloc.split(":")[0].lower().strip()
    relay_profile = RELAY_PROFILES.get(domain, {})

    # Playwright login => cookies+storage
    result = await asyncio.wait_for(
        login_and_get_cookies(
            service_url=service_url,
            username=(cred.username or recipient_email).strip(),
            password=password,
            profile=relay_profile,
        ),
        timeout=90,
    )

    # Now store in your existing handoff store from sharing.py to reuse /sharing/handoff/{session_id}
    from backend.routers.sharing import _handoff_store_put, COOKIE_HANDOFF_TTL_SECONDS

    session_id = _handoff_store_put(
        service_url=service_url,
        cookies=result.get("cookies", []),
        current_url=result.get("current_url"),
        localStorage=result.get("localStorage"),
        sessionStorage=result.get("sessionStorage"),
    )

    # Audit
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    shared.add_access_log_entry(ip, ua)
    db.commit()

    return {
        "message": "OK. Relay login done. Use extension handoff session_id (no secret revealed).",
        "handoff": {
            "session_id": session_id,
            "expires_in": COOKIE_HANDOFF_TTL_SECONDS,
        },
        "service_url": service_url,
        "current_url": result.get("current_url"),
    }