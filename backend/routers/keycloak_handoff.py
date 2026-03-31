import asyncio
import json
import os
import secrets
import time
import urllib.parse
from threading import Lock
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth.keycloak_auth import verify_keycloak_token
from backend.crypto.encryption import ShareEncryptor
from backend.integrations.keycloak_device_flow import KeycloakDeviceFlow
from backend.models.database import Credential, SharedAccess, User, get_db
from backend.relay.playwright_relay import login_and_get_cookies
from backend.routers.auth import get_current_user

# We reuse your RELAY_PROFILES and your handoff store (already working for extension)
from backend.routers.sharing import RELAY_PROFILES, _handoff_store_put, COOKIE_HANDOFF_TTL_SECONDS

router = APIRouter(prefix="/keycloak-sharing/handoff", tags=["Keycloak Passwordless Handoff"])

KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "zkp-realm")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "zkp-device-client")

flow = KeycloakDeviceFlow(
    base_url=KEYCLOAK_URL,
    realm=KEYCLOAK_REALM,
    client_id=KEYCLOAK_CLIENT_ID,
    timeout=int(os.getenv("KEYCLOAK_DEVICE_TIMEOUT", "180")),
)

# ---- In-memory store: kc_session_id -> device_code/share_id/created_at ----
KC_SESSION_STORE: dict[str, dict] = {}
KC_SESSION_LOCK = Lock()
KC_SESSION_TTL_SECONDS = 600  # 10 min


def _kc_cleanup(now: float):
    for sid, v in list(KC_SESSION_STORE.items()):
        if now - v.get("created_at", now) > KC_SESSION_TTL_SECONDS:
            KC_SESSION_STORE.pop(sid, None)


def _kc_put(data: dict) -> str:
    sid = secrets.token_urlsafe(24)
    now = time.time()
    with KC_SESSION_LOCK:
        _kc_cleanup(now)
        data["created_at"] = now
        KC_SESSION_STORE[sid] = data
    return sid


def _kc_consume(sid: str) -> dict | None:
    now = time.time()
    with KC_SESSION_LOCK:
        v = KC_SESSION_STORE.get(sid)
        if not v:
            return None
        if now - v.get("created_at", now) > KC_SESSION_TTL_SECONDS:
            KC_SESSION_STORE.pop(sid, None)
            return None
        return KC_SESSION_STORE.pop(sid, None)


class HandoffStartResponse(BaseModel):
    kc_session_id: str
    expires_in: int
    recipient_link: str
    device_flow: Dict[str, Any]


@router.post("/start", response_model=HandoffStartResponse)
def start_keycloak_handoff(
    share_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Owner starts device flow for a specific share_id (no password revealed).
    Returns a recipient_link to share (or QR).
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

    # Start Keycloak device flow
    df = flow.start()  # returns device_code/user_code/verification_uri/...
    if "device_code" not in df:
        raise HTTPException(status_code=400, detail=f"Keycloak device flow start failed: {df}")

    kc_session_id = _kc_put(
        {
            "share_id": share_id,
            "owner_id": current_user.id,
            "device_code": df["device_code"],
            "interval": int(df.get("interval", 5)),
        }
    )

    base = str(request.base_url).rstrip("/")
    recipient_link = f"{base}/keycloak-sharing/handoff/complete/{kc_session_id}"

    return {
        "kc_session_id": kc_session_id,
        "expires_in": KC_SESSION_TTL_SECONDS,
        "recipient_link": recipient_link,
        "device_flow": {
            "verification_uri": df.get("verification_uri"),
            "verification_uri_complete": df.get("verification_uri_complete"),
            "user_code": df.get("user_code"),
            "expires_in": df.get("expires_in"),
            "interval": df.get("interval", 5),
        },
    }


@router.get("/complete/{kc_session_id}")
def complete_keycloak_handoff(kc_session_id: str):
    """
    A simple landing endpoint that recipient opens.
    Your extension (or lightweight client) can read kc_session_id from URL and call /finalize.
    """
    return {
        "kc_session_id": kc_session_id,
        "message": "Open this link in the browser with the extension. The extension should call /finalize automatically.",
        "finalize_endpoint": f"/keycloak-sharing/handoff/finalize/{kc_session_id}",
    }


@router.post("/finalize/{kc_session_id}")
async def finalize_keycloak_handoff(
    kc_session_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Recipient-side finalize:
    - Poll Keycloak token using stored device_code
    - Verify Keycloak token (JWKS)
    - Enforce recipient_email matches share.recipient_email
    - Decrypt password server-side
    - Run Playwright login server-side
    - Store cookies/storage server-side => return /sharing/handoff/{session_id}
    """
    sess = _kc_consume(kc_session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="kc_session not found or expired")

    device_code = sess["device_code"]
    interval = int(sess.get("interval", 5))

    # Poll Keycloak for token
    try:
        token_data = flow.poll_for_token(device_code, interval=interval)
    except TimeoutError as e:
        raise HTTPException(status_code=408, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=403, detail=str(e))

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=403, detail="No access_token returned by Keycloak")

    # Verify Keycloak token via JWKS
    claims = verify_keycloak_token(access_token)
    recipient_email = (claims.get("email") or "").strip().lower()
    if not recipient_email:
        raise HTTPException(status_code=401, detail="Keycloak token missing email claim")

    share_id = int(sess["share_id"])
    shared = db.query(SharedAccess).filter(SharedAccess.id == share_id).first()
    if not shared:
        raise HTTPException(status_code=404, detail="Share not found")

    if shared.is_revoked:
        raise HTTPException(status_code=403, detail="Share revoked")

    if time.time() > (shared.expires_at or 0):
        raise HTTPException(status_code=403, detail="Share expired")

    if (shared.recipient_email or "").strip().lower() != recipient_email:
        raise HTTPException(status_code=403, detail="Recipient email mismatch")

    # Load credential metadata
    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    service_url = (cred.service_url or "").strip()
    if not service_url:
        raise HTTPException(status_code=400, detail="Credential has no service_url")

    # Decrypt shared secret server-side (recipient never sees password)
    try:
        encrypted_data = json.loads(shared.encrypted_payload or "{}")
        plaintext = ShareEncryptor.decrypt_from_share(encrypted_data, shared.share_key) or ""
        payload = json.loads(plaintext) if plaintext.strip().startswith("{") else {"password": plaintext}
        password = (payload.get("password") or "").strip()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot decrypt share payload: {e}")

    if not password:
        raise HTTPException(status_code=400, detail="Share payload missing password")

    # Pick relay profile by domain (same strategy as sharing.py)
    domain = urllib.parse.urlparse(service_url).netloc.split(":")[0].lower().strip()
    relay_profile = RELAY_PROFILES.get(domain, {})

    # Playwright login => cookies + localStorage + sessionStorage
    try:
        result = await asyncio.wait_for(
            login_and_get_cookies(
                service_url=service_url,
                username=(cred.username or recipient_email).strip(),
                password=password,
                profile=relay_profile,
            ),
            timeout=90,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Relay login timed out")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Relay login failed: {e}")

    # Store handoff server-side (extension will fetch and inject)
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
        "message": "OK. Relay login done. Use handoff session_id with browser extension. Secret never revealed.",
        "handoff": {"session_id": session_id, "expires_in": COOKIE_HANDOFF_TTL_SECONDS},
        "service_url": service_url,
        "current_url": result.get("current_url"),
    }