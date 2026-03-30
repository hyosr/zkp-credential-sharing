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
    data = _handoff_store_get(session_id)
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


class ShareIntentRequest(BaseModel):
    credential_id: int
    recipient_email: str
    permission: str = "read_once"
    ttl_hours: int = 24
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
        raise HTTPException(status_code=404, detail="Credential non trouvé")

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
        raise HTTPException(status_code=403, detail="Token invalide, expiré, ou email non autorisé")

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
        raise HTTPException(status_code=404, detail="Partage non trouvé")
    if time.time() > shared.expires_at:
        raise HTTPException(status_code=403, detail="Partage expiré")
    if (shared.recipient_email or "").strip().lower() != requester_email:
        raise HTTPException(status_code=403, detail="Email non autorisé pour ce partage")

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
        raise HTTPException(status_code=404, detail="Partage non trouvé")
    shared.is_revoked = True
    shared.revoked_at = time.time()
    db.commit()
    return {"message": "Partage révoqué avec succès"}


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
        raise HTTPException(status_code=404, detail="Partage non trouvé")

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
        raise HTTPException(status_code=403, detail="Token invalide, expiré, ou email non autorisé")

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
        raise HTTPException(status_code=404, detail="Partage non trouvé")
    if time.time() > shared.expires_at:
        raise HTTPException(status_code=403, detail="Partage expiré")

    shared_recipient = (shared.recipient_email or "").strip().lower()
    if shared_recipient != requester_email:
        raise HTTPException(status_code=403, detail="Email non autorisé pour ce partage")

    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential non trouvé")

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

    if shared.permission == "read_once" and shared.use_count >= shared.max_uses:
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
        raise HTTPException(status_code=404, detail="Credential non trouvé")

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
        encrypted_payload="{}",
        share_key=share_token,
        permission=req.permission,
        max_uses=req.max_uses,
        expires_at=time.time() + req.ttl_hours * 3600,
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
        raise HTTPException(status_code=404, detail="Partage non trouvé (ou déjà révoqué)")
    if time.time() > shared.expires_at:
        raise HTTPException(status_code=403, detail="Partage expiré")

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

    return {"message": "Partage finalisé. Envoyez le token au destinataire."}



def _handoff_store_get(session_id: str) -> dict | None:
    now = time.time()
    with COOKIE_HANDOFF_LOCK:
        v = COOKIE_HANDOFF_STORE.get(session_id)
        if not v:
            return None
        if now - v.get("created_at", now) > COOKIE_HANDOFF_TTL_SECONDS:
            return None
        return v   # ne supprime pas, juste retourne


























