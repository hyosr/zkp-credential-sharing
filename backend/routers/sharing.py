"""
Sharing Router - Zero-Knowledge Secure Credential Sharing
==========================================================
Partage de credentials sans jamais révéler le mot de passe original.

Architecture Zero-Knowledge :
1. Le propriétaire déchiffre le credential côté CLIENT
2. Il re-chiffre avec une clé éphémère one-time générée côté serveur
3. Le destinataire reçoit un token pour accéder au blob chiffré
4. Le serveur stocke uniquement le hash du token + le blob chiffré
5. Ni le serveur, ni les logs ne contiennent le secret en clair
"""

import asyncio
import hashlib
import json
import secrets
import time
import urllib.parse
from threading import Lock
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.crypto.encryption import ShareEncryptor
from backend.crypto.token_manager import (
    generate_share_token,
    validate_and_consume_token,
    validate_token,
)
from backend.models.database import Credential, SharedAccess, User, get_db
from backend.relay.playwright_relay import login_and_get_cookies
from backend.routers.auth import get_current_user
from backend.schemas.sharing import RelayLoginRequest


router = APIRouter(prefix="/sharing", tags=["Secure Sharing"])
encryptor = ShareEncryptor()


MAX_TTL_MINUTES = 24 * 60   # ✅ exemple: 24 heures max
MIN_TTL_MINUTES = 1         # ✅ minimum 1 minute



# ─── Handoff store (ONE source of truth) ───────────────────────────────────────
COOKIE_HANDOFF_STORE: dict[str, dict] = {}
COOKIE_HANDOFF_LOCK = Lock()
COOKIE_HANDOFF_TTL_SECONDS = 600  # 10 minutes


def _handoff_cleanup(now: float):
    for sid, v in list(COOKIE_HANDOFF_STORE.items()):
        if now - v.get("created_at", now) > COOKIE_HANDOFF_TTL_SECONDS:
            COOKIE_HANDOFF_STORE.pop(sid, None)


def _handoff_store_put(
    service_url: str,
    cookies: list,
    *,

    current_url: str | None = None,
    localStorage: str | None = None,
    sessionStorage: str | None = None,
) -> str:
    session_id = secrets.token_urlsafe(24)
    now = time.time()
    with COOKIE_HANDOFF_LOCK:
        _handoff_cleanup(now)
        COOKIE_HANDOFF_STORE[session_id] = {
            "service_url": service_url,
            "current_url": current_url or service_url,
            "cookies": cookies,
            "localStorage": localStorage,
            "sessionStorage": sessionStorage,
            "created_at": now,
        }
    return session_id


def _handoff_store_consume(session_id: str) -> dict | None:
    now = time.time()
    with COOKIE_HANDOFF_LOCK:
        v = COOKIE_HANDOFF_STORE.get(session_id)
        if not v:
            return None
        if now - v.get("created_at", now) > COOKIE_HANDOFF_TTL_SECONDS:
            COOKIE_HANDOFF_STORE.pop(session_id, None)
            return None
        return COOKIE_HANDOFF_STORE.pop(session_id, None)


@router.get("/handoff/{session_id}")
def handoff_get(session_id: str):
    """
    One-time handoff endpoint for the browser extension.
    Returns cookies + localStorage + sessionStorage + current_url.
    Consumed on first read.
    """
    data = _handoff_store_consume(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Handoff session not found or expired")


    return {
        "service_url": data.get("service_url"),
        "current_url": data.get("current_url"),
        "cookies": data.get("cookies", []),
        "localStorage": data.get("localStorage"),
        "sessionStorage": data.get("sessionStorage"),
        "expires_in": COOKIE_HANDOFF_TTL_SECONDS,
    }


# ─── Relay Profiles ───────────────────────────────────────────────────────────
RELAY_PROFILES = {
    "recolyse.com": {
        "username_selector": [
            "#outlined-basic",
            "input[type='email']",
            "input[name='email']",
            "input#email",
            "input[autocomplete='email']",
            "input[type='text']",
        ],
        "password_selector": [
            "input[type='password'].MuiInputBase-input",
            "input[type='password']",
            "input[name='password']",
            "input#password",
            "input[autocomplete='current-password']",
        ],
        "submit_selector": [
            "button.style_primary-btn__aHK9J",
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Se connecter')",
            "button:has-text('Connexion')",
            "button:has-text('Login')",
            "button:has-text('Sign in')",
        ],
        "post_login_wait": 2000,
        "post_login_timeout_ms": 15000,
        "post_login_selector": "text=Logout, text=Se déconnecter, [aria-label*='account'], [data-testid*='avatar']",
    }
}


# ─── Schemas ──────────────────────────────────────────────────────────────────
class ShareRequest(BaseModel):
    credential_id: int
    recipient_email: str
    permission: str = "read_once"  # "read" | "read_once"
    ttl_hours: int = 24
    max_uses: int = 1
    encrypted_payload: str  # JSON: {nonce, ciphertext}
    share_key_token: str


class AccessShareRequest(BaseModel):
    token: str


# class ShareIntentRequest(BaseModel):
#     credential_id: int
#     recipient_email: str
#     permission: str = "read_once"
#     ttl_hours: int = 24
#     max_uses: int = 1



class ShareIntentRequest(BaseModel):
    credential_id: int
    recipient_email: str
    permission: str = "read_once"
    # Backward compatible field (old UI)
    ttl_hours: int | None = None
    # ✅ New: minutes-based TTL
    ttl_minutes: int | None = 60
    max_uses: int = 1


class ShareIntentResponse(BaseModel):
    message: str
    share_token: str
    share_id: int
    expires_at: float
    recipient: str
    permission: str


class ShareFinalizeRequest(BaseModel):
    token: str
    encrypted_payload: str


# ─── Routes ───────────────────────────────────────────────────────────────────
@router.post("/create")
def create_share(
    req: ShareRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(Credential)
        .filter(
            Credential.id == req.credential_id,
            Credential.owner_id == current_user.id,
            Credential.is_active == True,
        )
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    share_token = generate_share_token(
        credential_id=req.credential_id,
        owner_id=current_user.id,
        recipient_email=req.recipient_email,
        permission=req.permission,
        ttl_hours=req.ttl_hours,
        max_uses=req.max_uses,
    )

    token_hash = hashlib.sha256(share_token.encode()).hexdigest()

    shared = SharedAccess(
        credential_id=req.credential_id,
        owner_id=current_user.id,
        recipient_email=req.recipient_email,
        token_hash=token_hash,
        encrypted_payload=req.encrypted_payload,
        share_key=req.share_key_token,
        permission=req.permission,
        max_uses=req.max_uses,
        expires_at=time.time() + req.ttl_hours * 3600,
        created_at=time.time(),
    )
    db.add(shared)
    db.commit()
    db.refresh(shared)

    return {
        "message": "Partage créé avec succès (Zero-Knowledge)",
        "share_token": share_token,
        "share_id": shared.id,
        "expires_at": shared.expires_at,
        "recipient": req.recipient_email,
        "permission": req.permission,
    }


@router.post("/access")
def access_share(
    req: AccessShareRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    requester_email = (current_user.email or "").strip().lower()
    if not requester_email:
        raise HTTPException(status_code=401, detail="Authenticated user email missing")

    share_info = validate_token(req.token, requester_email)
    if not share_info:
        raise HTTPException(status_code=403, detail="Token invalid, expired, or email not authorized")

    token_hash = hashlib.sha256(req.token.encode()).hexdigest()
    shared = (
        db.query(SharedAccess)
        .filter(
            SharedAccess.token_hash == token_hash,
            SharedAccess.is_revoked == False,
        )
        .first()
    )

    if not shared:
        raise HTTPException(status_code=404, detail="Share not found")
    if time.time() > shared.expires_at:
        raise HTTPException(status_code=403, detail="Share expired")
    if (shared.recipient_email or "").strip().lower() != requester_email:
        raise HTTPException(status_code=403, detail="Email not authorized for this share")

    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    shared.add_access_log_entry(ip, ua)
    db.commit()

    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()

    return {
        "credential_name": cred.name if cred else "Inconnu",
        "service_url": cred.service_url if cred else None,
        "username": cred.username if cred else None,
        "permission": shared.permission,
        "expires_at": shared.expires_at,
        "use_count": shared.use_count,
        "max_uses": shared.max_uses,
        "message": "Access verified. Use /sharing/relay-login to login without revealing the password.",
        "next_action": "relay_login",
    }


@router.get("/my-shares")
def list_my_shares(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    shares = (
        db.query(SharedAccess)
        .filter(
            SharedAccess.owner_id == current_user.id,
            SharedAccess.is_revoked == False,
        )
        .all()
    )

    result = []
    for s in shares:
        cred = db.query(Credential).filter(Credential.id == s.credential_id).first()
        result.append(
            {
                "share_id": s.id,
                "credential_name": cred.name if cred else "?",
                "recipient_email": s.recipient_email,
                "permission": s.permission,
                "use_count": s.use_count,
                "max_uses": s.max_uses,
                "expires_at": s.expires_at,
                "created_at": s.created_at,
                "is_expired": time.time() > s.expires_at,
                "token_hash_preview": s.token_hash[:8] + "...",
            }
        )
    return result







class IncreaseMaxUsesRequest(BaseModel):
    add_uses: int = 1

@router.post("/increase-max-uses/{share_id}")
def increase_max_uses(
    share_id: int,
    payload: IncreaseMaxUsesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Owner can increment max_uses for an existing share (without changing token).
    Useful if you want to extend usage after creation.
    """
    if payload.add_uses <= 0:
        raise HTTPException(status_code=400, detail="add_uses must be > 0")

    shared = (
        db.query(SharedAccess)
        .filter(
            SharedAccess.id == share_id,
            SharedAccess.owner_id == current_user.id,
        )
        .first()
    )
    if not shared:
        raise HTTPException(status_code=404, detail="Share not found")

    # Do not allow changes if already expired
    if time.time() > shared.expires_at:
        raise HTTPException(status_code=400, detail="Share is expired")

    shared.max_uses += int(payload.add_uses)

    # If it was revoked because it reached max uses, you may optionally "unrevoke" it
    # ONLY if you want that behavior:
    if shared.is_revoked and shared.use_count < shared.max_uses:
        shared.is_revoked = False
        shared.revoked_at = None

    db.commit()
    db.refresh(shared)

    return {
        "message": "max_uses increased",
        "share_id": shared.id,
        "use_count": shared.use_count,
        "max_uses": shared.max_uses,
        "is_revoked": shared.is_revoked,
        "expires_at": shared.expires_at,
    }















@router.delete("/revoke/{share_id}")
def revoke_share(
    share_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    shared = (
        db.query(SharedAccess)
        .filter(
            SharedAccess.id == share_id,
            SharedAccess.owner_id == current_user.id,
        )
        .first()
    )
    if not shared:
        raise HTTPException(status_code=404, detail="Share not found")
    shared.is_revoked = True
    shared.revoked_at = time.time()
    db.commit()
    return {"message": "Share revoked successfully"}


@router.get("/audit/{share_id}")
def get_audit_log(
    share_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    shared = (
        db.query(SharedAccess)
        .filter(
            SharedAccess.id == share_id,
            SharedAccess.owner_id == current_user.id,
        )
        .first()
    )
    if not shared:
        raise HTTPException(status_code=404, detail="Share not found")

    log = json.loads(shared.access_log or "[]")
    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()

    return {
        "share_id": share_id,
        "credential_name": cred.name if cred else "?",
        "recipient_email": shared.recipient_email,
        "created_at": shared.created_at,
        "expires_at": shared.expires_at,
        "use_count": shared.use_count,
        "is_revoked": shared.is_revoked,
        "access_log": log,
    }


@router.post("/relay-login")
async def relay_login(
    req: RelayLoginRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    requester_email = (current_user.email or "").strip().lower()
    if not requester_email:
        raise HTTPException(status_code=401, detail="Authenticated user email missing")

    # 1) Validate token against authenticated email (zero-trust)
    share_info = validate_and_consume_token(req.token, requester_email)
    if not share_info:
        raise HTTPException(status_code=403, detail="Token invalid, expired, or email not authorized")

    # 2) Load share from DB
    token_hash = hashlib.sha256(req.token.encode()).hexdigest()
    shared = (
        db.query(SharedAccess)
        .filter(
            SharedAccess.token_hash == token_hash,
            SharedAccess.is_revoked == False,
        )
        .first()
    )

    if not shared:
        raise HTTPException(status_code=404, detail="Share not found")
    if time.time() > shared.expires_at:
        raise HTTPException(status_code=403, detail="Share expired")

    shared_recipient = (shared.recipient_email or "").strip().lower()
    if shared_recipient != requester_email:
        raise HTTPException(status_code=403, detail="Email not authorized for this share")

    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    service_url = (req.service_url_override or (cred.service_url or "")).strip()
    if not service_url:
        raise HTTPException(status_code=400, detail="service_url missing on credential")

    domain = urllib.parse.urlparse(service_url).netloc.split(":")[0].lower().strip()
    relay_profile = RELAY_PROFILES.get(domain, {})

    # 3) decrypt payload (password)
    try:
        encrypted_data = json.loads(shared.encrypted_payload)
    except Exception:
        raise HTTPException(status_code=400, detail="encrypted_payload must be JSON string {nonce,ciphertext}")

    try:
        plaintext = ShareEncryptor.decrypt_from_share(encrypted_data, shared.share_key) or ""
        payload = json.loads(plaintext) if plaintext.strip().startswith("{") else {"password": plaintext}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot decrypt share payload: {e}")

    password = (payload.get("password") or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="Share payload missing 'password'")

    # payload relay_profile can override
    if payload.get("relay_profile"):
        relay_profile = payload.get("relay_profile") or relay_profile

    # 4) run Playwright
    try:
        result = await asyncio.wait_for(
            login_and_get_cookies(
                service_url=service_url,
                username=(cred.username or requester_email).strip(),
                password=password,
                profile=relay_profile,
            ),
            timeout=90,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Relay login timed out (Playwright took too long)")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Relay login failed: {e}")

    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Relay login returned invalid result type")

    # 5) create handoff session (store cookies + storages server-side)
    cookies = result.get("cookies", [])
    session_id = _handoff_store_put(
        service_url=service_url,
        cookies=cookies,
        current_url=result.get("current_url"),
        localStorage=result.get("localStorage"),
        sessionStorage=result.get("sessionStorage"),
    )

    # 6) audit + revoke logic
    shared.use_count += 1
    shared.used_at = time.time()
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    shared.add_access_log_entry(ip, ua)

    # if shared.permission == "read_once" and shared.use_count >= shared.max_uses:
    #     shared.is_revoked = True
    #     shared.revoked_at = time.time()

    if shared.use_count >= shared.max_uses:
        shared.is_revoked = True
        shared.revoked_at = time.time()

    db.commit()

    # IMPORTANT: do NOT return cookies to frontend
    return {
        "credential_name": cred.name,
        "service_url": service_url,
        "username": cred.username,
        "relay": {
            "current_url": result.get("current_url"),
            "title": result.get("title"),
            "used_selectors": result.get("used_selectors"),
            "login_detected": result.get("login_detected", False),
        },
        "handoff": {"session_id": session_id, "expires_in": COOKIE_HANDOFF_TTL_SECONDS},
        "message": "Relay login done. Cookies+storages stored server-side for extension handoff.",
    }


@router.post("/create-intent", response_model=ShareIntentResponse)
def create_share_intent(
    req: ShareIntentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cred = (
        db.query(Credential)
        .filter(
            Credential.id == req.credential_id,
            Credential.owner_id == current_user.id,
            Credential.is_active == True,
        )
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    


        # ---- TTL handling (minutes, with backward compatibility) ----
    ttl_minutes = req.ttl_minutes

    # If old client only sends ttl_hours, convert it
    if (ttl_minutes is None or ttl_minutes <= 0) and req.ttl_hours is not None:
        ttl_minutes = int(req.ttl_hours) * 60

    if ttl_minutes is None:
        ttl_minutes = 60

    if ttl_minutes < MIN_TTL_MINUTES:
        raise HTTPException(status_code=400, detail=f"ttl_minutes must be >= {MIN_TTL_MINUTES}")

    if ttl_minutes > MAX_TTL_MINUTES:
        raise HTTPException(
            status_code=400,
            detail=f"ttl_minutes too large (max {MAX_TTL_MINUTES} minutes)",
        )

    ttl_seconds = int(ttl_minutes) * 60





    # share_token = generate_share_token(
    #     credential_id=req.credential_id,
    #     owner_id=current_user.id,
    #     recipient_email=req.recipient_email,
    #     permission=req.permission,
    #     ttl_hours=req.ttl_hours,
    #     max_uses=req.max_uses,
    # )



    # Keep generate_share_token signature (ttl_hours) by converting back safely
    ttl_hours_for_token = max(1, int((ttl_seconds + 3599) // 3600))

    share_token = generate_share_token(
        credential_id=req.credential_id,
        owner_id=current_user.id,
        recipient_email=req.recipient_email,
        permission=req.permission,
        ttl_hours=ttl_hours_for_token,
        max_uses=req.max_uses,
    )




    token_hash = hashlib.sha256(share_token.encode()).hexdigest()

    shared = SharedAccess(
        credential_id=req.credential_id,
        owner_id=current_user.id,
        recipient_email=req.recipient_email,
        token_hash=token_hash,
        encrypted_payload="{}",
        share_key=share_token,
        permission=req.permission,
        max_uses=req.max_uses,
        # expires_at=time.time() + req.ttl_hours * 3600,
        expires_at=time.time() + ttl_seconds,
        created_at=time.time(),
    )
    db.add(shared)
    db.commit()
    db.refresh(shared)

    return {
        "message": "Intent créé. Chiffrez localement avec share_token puis finalisez.",
        "share_token": share_token,
        "share_id": shared.id,
        "expires_at": shared.expires_at,
        "recipient": req.recipient_email,
        "permission": req.permission,
    }


@router.post("/finalize")
def finalize_share(
    req: ShareFinalizeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    token_hash = hashlib.sha256(req.token.encode()).hexdigest()

    shared = (
        db.query(SharedAccess)
        .filter(
            SharedAccess.token_hash == token_hash,
            SharedAccess.owner_id == current_user.id,
            SharedAccess.is_revoked == False,
        )
        .first()
    )

    if not shared:
        raise HTTPException(status_code=404, detail="Share not found (or already revoked)")
    if time.time() > shared.expires_at:
        raise HTTPException(status_code=403, detail="Share expired")

    try:
        d = json.loads(req.encrypted_payload)
        if "nonce" not in d or "ciphertext" not in d:
            raise ValueError("missing nonce/ciphertext")
    except Exception:
        raise HTTPException(status_code=400, detail="encrypted_payload must be JSON {nonce,ciphertext}")

    shared.encrypted_payload = req.encrypted_payload
    db.commit()

    if not shared.share_key:
        raise HTTPException(status_code=500, detail="Share has no share_key set (data integrity error)")

    return {"message": "Share finalized. Send the token to the recipient."}



def _handoff_store_get(session_id: str) -> dict | None:
    now = time.time()
    with COOKIE_HANDOFF_LOCK:
        v = COOKIE_HANDOFF_STORE.get(session_id)
        if not v:
            return None
        if now - v.get("created_at", now) > COOKIE_HANDOFF_TTL_SECONDS:
            return None
        return v   # ne supprime pas, juste retourne












# ========== Owner‑Assisted Relay Login ==========
ASSISTED_REQUESTS: dict[str, dict] = {}
ASSISTED_TTL = 600  # 10 minutes

class AssistedRequestPayload(BaseModel):
    share_token: str

class AssistedSessionPayload(BaseModel):
    cookies: list
    localStorage: str | None = None
    sessionStorage: str | None = None
    current_url: str | None = None

@router.post("/assisted/request")
def assisted_create_request(
    payload: AssistedRequestPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Destinataire initie une demande d'assistance."""
    requester_email = (current_user.email or "").strip().lower()
    if not requester_email:
        raise HTTPException(401, "Authenticated user email missing")

    # Valider le share token
    share_info = validate_token(payload.share_token, requester_email)
    if not share_info:
        raise HTTPException(403, "Token invalide, expiré, ou email non autorisé")

    token_hash = hashlib.sha256(payload.share_token.encode()).hexdigest()
    shared = db.query(SharedAccess).filter(
        SharedAccess.token_hash == token_hash,
        SharedAccess.is_revoked == False,
    ).first()
    if not shared or time.time() > shared.expires_at:
        raise HTTPException(404, "Share not found or expired")

    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()
    if not cred:
        raise HTTPException(404, "Credential not found")

    service_url = cred.service_url or ""
    if not service_url:
        raise HTTPException(400, "Credential missing service_url")

    request_id = secrets.token_urlsafe(16)
    ASSISTED_REQUESTS[request_id] = {
        "owner_id": shared.owner_id,
        "recipient_id": current_user.id,
        "recipient_email": requester_email,
        "service_url": service_url,
        "status": "pending",  # pending -> approved -> session_received -> completed
        "handoff_session_id": None,
        "created_at": time.time(),
        "expires_at": time.time() + ASSISTED_TTL,
    }
    return {"request_id": request_id, "status": "pending", "expires_at": ASSISTED_REQUESTS[request_id]["expires_at"]}

@router.get("/assisted/pending")
def assisted_pending_requests(current_user: User = Depends(get_current_user)):
    """Propriétaire : liste des demandes en attente le concernant."""
    now = time.time()
    result = []
    for rid, req in ASSISTED_REQUESTS.items():
        if req["owner_id"] == current_user.id and req["status"] == "pending" and req["expires_at"] > now:
            result.append({
                "request_id": rid,
                "service_url": req["service_url"],
                "recipient_email": req["recipient_email"],
                "expires_at": req["expires_at"],
            })
    return result

@router.post("/assisted/{request_id}/approve")
def assisted_approve(request_id: str, current_user: User = Depends(get_current_user)):
    """Propriétaire approuve la demande → retourne l'URL de login où il devra se connecter."""
    req = ASSISTED_REQUESTS.get(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    if req["owner_id"] != current_user.id:
        raise HTTPException(403, "Not allowed")
    if req["expires_at"] <= time.time():
        req["status"] = "expired"
        raise HTTPException(400, "Request expired")
    if req["status"] != "pending":
        raise HTTPException(400, f"Invalid status: {req['status']}")

    req["status"] = "approved"
    # L'extension ouvrira cette URL (le site cible) pour que le propriétaire se connecte manuellement
    assist_login_url = req["service_url"]
    return {"status": "approved", "assist_login_url": assist_login_url}





# @router.post("/assisted/{request_id}/session")
# def assisted_submit_session(
#     request_id: str,
#     session_data: AssistedSessionPayload,
#     current_user: User = Depends(get_current_user),
# ):
#     """Propriétaire envoie les cookies+storages après s'être connecté manuellement."""
#     req = ASSISTED_REQUESTS.get(request_id)
#     if not req:
#         raise HTTPException(404, "Request not found")
#     if req["owner_id"] != current_user.id:
#         raise HTTPException(403, "Not allowed")
#     if req["status"] != "approved":
#         raise HTTPException(400, f"Invalid status: {req['status']}")

#     current_url = session_data.current_url or req["service_url"]

#     # Stocker la session dans le handoff store existant
#     handoff_session_id = _handoff_store_put(
#         service_url=req["service_url"],
#         cookies=session_data.cookies,
#         localStorage=session_data.localStorage,
#         sessionStorage=session_data.sessionStorage,
#         current_url=current_url,
#     )
#     req["status"] = "completed"
#     req["handoff_session_id"] = handoff_session_id
#     return {"handoff_session_id": handoff_session_id}





@router.post("/assisted/{request_id}/session")
def assisted_submit_session(
    request_id: str,
    session_data: AssistedSessionPayload,
    current_user: User = Depends(get_current_user),
):
    """Propriétaire envoie les cookies+storages après s'être connecté manuellement."""
    req = ASSISTED_REQUESTS.get(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    if req["owner_id"] != current_user.id:
        raise HTTPException(403, "Not allowed")
    if req["status"] != "approved":
        raise HTTPException(400, f"Invalid status: {req['status']}")

    # Utiliser l'URL fournie par l'extension, sinon celle du credential
    current_url = session_data.current_url or req["service_url"]





    # Si l'URL fournie est encore une page de login, on la corrige
    # if current_url.endswith('/auth') or current_url.endswith('/login'):
    #     from urllib.parse import urlparse
    #     parsed = urlparse(current_url)
    #     current_url = f"{parsed.scheme}://{parsed.netloc}/"
    #     print(f"URL corrigée par le backend: {current_url}")



    # Stocker la session dans le handoff store existant
    handoff_session_id = _handoff_store_put(
        service_url=req["service_url"],
        cookies=session_data.cookies,


        localStorage=session_data.localStorage,
        sessionStorage=session_data.sessionStorage,
        current_url=current_url,  # Utilisation de l'URL corrigée
    )
    req["status"] = "completed"
    req["handoff_session_id"] = handoff_session_id






    return {"handoff_session_id": handoff_session_id}








# @router.get("/assisted/{request_id}/status")
# def assisted_status(request_id: str, current_user: User = Depends(get_current_user)):
#     """Destinataire interroge le statut. Une fois completed, retourne handoff_session_id."""
#     req = ASSISTED_REQUESTS.get(request_id)
#     if not req:
#         raise HTTPException(404, "Request not found")
#     if current_user.id not in (req["owner_id"], req["recipient_id"]):
#         raise HTTPException(403, "Not allowed")

#     if req["expires_at"] <= time.time() and req["status"] in ("pending", "approved"):
#         req["status"] = "expired"

#     return {
#         "status": req["status"],
#         "handoff_session_id": req.get("handoff_session_id"),
#         "expires_at": req["expires_at"],
#     }




@router.get("/assisted/{request_id}/status")
def assisted_status(request_id: str, current_user: User = Depends(get_current_user)):
    req = ASSISTED_REQUESTS.get(request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    if current_user.id not in (req["owner_id"], req["recipient_id"]):
        raise HTTPException(403, "Not allowed")

    if req["expires_at"] <= time.time() and req["status"] in ("pending", "approved"):
        req["status"] = "expired"

    handoff_url = None
    if req["status"] == "completed" and req.get("handoff_session_id"):
        # Construire l'URL complète ici
        handoff_url = f"/sharing/handoff/{req['handoff_session_id']}"

    return {
        "status": req["status"],
        "handoff_session_id": req.get("handoff_session_id"),
        "handoff_url": handoff_url,  # ← AJOUTER
        "expires_at": req["expires_at"],
    }





























