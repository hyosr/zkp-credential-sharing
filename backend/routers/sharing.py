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
import time
from typing import List, Optional,  Dict, Any
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.schemas.sharing import RelayLoginRequest
from backend.relay.playwright_relay import login_and_get_cookies

from backend.crypto.encryption import ShareEncryptor
from backend.crypto.token_manager import (
    generate_share_token,
    list_active_tokens,
    revoke_token,
    validate_and_consume_token,
    validate_token,
)
from backend.models.database import Credential, SharedAccess, User, get_db
from backend.routers.auth import get_current_user


from pydantic import BaseModel


from backend.models.database import get_db


import secrets
from threading import Lock

# Temporary in-memory store: session_id -> {"cookies": [...], "service_url": "...", "created_at": ...}
COOKIE_HANDOFF_STORE: dict[str, dict] = {}
COOKIE_HANDOFF_LOCK = Lock()
COOKIE_HANDOFF_TTL_SECONDS = 600  # 2 minutes



router = APIRouter(prefix="/sharing", tags=["Secure Sharing"])

encryptor = ShareEncryptor()





def _handoff_cleanup(now: float):
    for sid, v in list(COOKIE_HANDOFF_STORE.items()):
        if now - v.get("created_at", now) > COOKIE_HANDOFF_TTL_SECONDS:
            COOKIE_HANDOFF_STORE.pop(sid, None)


def _handoff_store_put(service_url: str, cookies: list) -> str:
    session_id = secrets.token_urlsafe(24)
    now = time.time()
    with COOKIE_HANDOFF_LOCK:
        _handoff_cleanup(now)
        COOKIE_HANDOFF_STORE[session_id] = {
            "service_url": service_url,
            "cookies": cookies,
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











# RELAY_PROFILES = {
#     "recolyse.com": {
#         "username_selector": "input[type='email']",
#         "password_selector": "input[type='password']",
#         "submit_selector": "button.style_primary-btn__aHK9J",
#         "post_login_wait": 2000,  # attendre 2s après soumission (optionnel)
#     },
#     # Vous pouvez ajouter d'autres domaines ici
# }



RELAY_PROFILES = {
    "recolyse.com": {
        "username_selector": "#outlined-basic",  # ID unique pour l'email
        "password_selector": "input[type='password'].MuiInputBase-input",  # classe + type
        "submit_selector": "button.style_primary-btn__aHK9J",
        "post_login_wait": 2000,  # attendre 2s après soumission

         #✅ NEW: selector that exists only when user is logged in
        # TODO: adjust to a real element in the logged-in UI if needed
        "post_login_selector": "text=Logout, text=Se déconnecter, [aria-label*='account'], [data-testid*='avatar']",
        "post_login_timeout_ms": 15000,

    },
    # autres domaines...
}










# async def login_and_get_cookies(
#     service_url: str,
#     username: str,
#     password: str,
#     profile: Optional[Dict[str, Any]] = None
# ) -> Dict[str, Any]:
#     """
#     Utilise Playwright pour effectuer le login sur service_url avec username/password.
#     Retourne les cookies, l'URL finale et le titre de la page.
#     """
#     from playwright.async_api import async_playwright

#     if profile is None:
#         profile = {}

#     username_selector = profile.get("username_selector", "input[type='email'], input[name='email']")
#     password_selector = profile.get("password_selector", "input[type='password']")
#     submit_selector = profile.get("submit_selector", "button[type='submit']")
#     post_login_wait = profile.get("post_login_wait", 0)

#     async with async_playwright() as p:
#         browser = await p.chromium.launch(headless=True)
#         page = await browser.new_page()
#         try:
#             await page.goto(service_url, wait_until="networkidle")

#             # Remplir les champs
#             await page.fill(username_selector, username)
#             await page.fill(password_selector, password)
#             await page.click(submit_selector)

#             if post_login_wait > 0:
#                 await page.wait_for_timeout(post_login_wait)
#             else:
#                 await page.wait_for_load_state("networkidle")

#             cookies = await page.context.cookies()
#             return {
#                 "cookies": cookies,
#                 "current_url": page.url,
#                 "title": await page.title(),
#                 "used_selectors": {
#                     "username": username_selector,
#                     "password": password_selector,
#                     "submit": submit_selector,
#                 }
#             }
#         except Exception as e:
#             raise Exception(f"Playwright login failed: {str(e)}")
#         finally:
#             await browser.close()














# ─── Schemas ──────────────────────────────────────────────────────────────────

class ShareRequest(BaseModel):
    credential_id: int
    recipient_email: str
    permission: str = "read_once"       # "read" | "read_once"
    ttl_hours: int = 24                 # Durée de validité
    max_uses: int = 1
    # Le propriétaire envoie le secret RE-CHIFFRÉ avec la clé éphémère
    # (déchiffrement côté client, re-chiffrement avec share_key)
    encrypted_payload: str              # JSON: {nonce, ciphertext}
    share_key_token: str                # Clé éphémère en base64 (sera consommée)


class AccessShareRequest(BaseModel):
    token: str
    # requester_email: str


class RevokeRequest(BaseModel):
    token_hash_preview: str             # Premier 8 chars du hash (depuis list_tokens)



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
    encrypted_payload: str  # JSON {nonce,ciphertext}





# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/create")
def create_share(
    req: ShareRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Crée un partage sécurisé Zero-Knowledge.
    Le secret est re-chiffré avec une clé éphémère.
    Le serveur stocke uniquement le blob chiffré + hash du token.
    """
    # Vérification propriétaire
    cred = db.query(Credential).filter(
        Credential.id == req.credential_id,
        Credential.owner_id == current_user.id,
        Credential.is_active == True,
    ).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential non trouvé")

    # Génère le token de partage sécurisé
    share_token = generate_share_token(
        credential_id=req.credential_id,
        owner_id=current_user.id,
        recipient_email=req.recipient_email,
        permission=req.permission,
        ttl_hours=req.ttl_hours,
        max_uses=req.max_uses,
    )

    # Hash du token pour stockage (jamais le token brut en base)
    token_hash = hashlib.sha256(share_token.encode()).hexdigest()

    # Stocke le partage en base (blob chiffré + métadonnées)
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
        "share_token": share_token,         # À envoyer au destinataire (hors bande)
        "share_id": shared.id,
        "expires_at": shared.expires_at,
        "recipient": req.recipient_email,
        "permission": req.permission,
    }


# @router.post("/access")
# def access_share(
#     req: AccessShareRequest,
#     request: Request,
#     db: Session = Depends(get_db),
# ):
#     """
#     Accès à un credential partagé via token one-time.
#     Zero-Trust : vérifie l'email du demandeur à chaque accès.
#     Retourne le blob chiffré (le destinataire le déchiffre avec le token).
#     """
#     # Validation Zero-Trust du token
#     share_info = validate_and_consume_token(req.token, req.requester_email)
#     if not share_info:
#         raise HTTPException(
#             status_code=403,
#             detail="Token invalide, expiré, ou email non autorisé"
#         )

#     # Récupère le partage en base
#     token_hash = hashlib.sha256(req.token.encode()).hexdigest()
#     shared = db.query(SharedAccess).filter(
#         SharedAccess.token_hash == token_hash,
#         SharedAccess.is_revoked == False,
#     ).first()
#     if not shared:
#         raise HTTPException(status_code=404, detail="Partage non trouvé")
#     if time.time() > shared.expires_at:
#         raise HTTPException(status_code=403, detail="Partage expiré")

#     # Audit trail
#     shared.use_count += 1
#     shared.used_at = time.time()
#     ip = request.client.host if request.client else "unknown"
#     ua = request.headers.get("user-agent", "unknown")
#     shared.add_access_log_entry(ip, ua)

#     # Si one-time, invalide après usage
#     if shared.permission == "read_once" and shared.use_count >= shared.max_uses:
#         shared.is_revoked = True
#         shared.revoked_at = time.time()

#     db.commit()

#     # Récupère infos du credential (sans le secret original)
#     cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()

#     return {
#         "credential_name": cred.name if cred else "Inconnu",
#         "service_url": cred.service_url if cred else None,
#         "username": cred.username if cred else None,
#         "encrypted_payload": shared.encrypted_payload,
#         # Le destinataire déchiffre avec son token (clé éphémère)
#         # "decryption_key": req.token,
#         "decryption_key": shared.share_key,
#         "permission": shared.permission,
#         "message": "Accès autorisé — déchiffrez localement avec le token fourni",
#     }






# @router.post("/access")
# def access_share(
#     req: AccessShareRequest,
#     request: Request,
#     current_user: User = Depends(get_current_user),  # ✅ obligatoire
#     db: Session = Depends(get_db),
# ):
#     """
#     Accès à un credential partagé via token one-time.
#     Zero-Trust : le demandeur DOIT être connecté, et son email doit matcher recipient_email.
#     Retourne le blob chiffré (le destinataire le déchiffre avec la clé stockée).
#     """
#     # ✅ Validation Zero-Trust: on utilise l'email du user connecté (pas de champ fourni par le client)
#     requester_email = current_user.email

#     share_info = validate_and_consume_token(req.token, requester_email)
#     if not share_info:
#         raise HTTPException(
#             status_code=403,
#             detail="Token invalide, expiré, ou email non autorisé"
#         )

#     # Récupère le partage en base
#     token_hash = hashlib.sha256(req.token.encode()).hexdigest()
#     shared = db.query(SharedAccess).filter(
#         SharedAccess.token_hash == token_hash,
#         SharedAccess.is_revoked == False,
#     ).first()
#     if not shared:
#         raise HTTPException(status_code=404, detail="Partage non trouvé")
#     if time.time() > shared.expires_at:
#         raise HTTPException(status_code=403, detail="Partage expiré")

#     # ✅ Double-check DB: email connecté doit matcher recipient_email (au cas où token_manager est reset)
#     if (shared.recipient_email or "").lower().strip() != (requester_email or "").lower().strip():
#         raise HTTPException(status_code=403, detail="Email non autorisé pour ce partage")

#     # Audit trail
#     shared.use_count += 1
#     shared.used_at = time.time()
#     ip = request.client.host if request.client else "unknown"
#     ua = request.headers.get("user-agent", "unknown")
#     shared.add_access_log_entry(ip, ua)

#     # Si one-time, invalide après usage
#     if shared.permission == "read_once" and shared.use_count >= shared.max_uses:
#         shared.is_revoked = True
#         shared.revoked_at = time.time()

#     db.commit()

#     # Récupère infos du credential (sans le secret original)
#     cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()

#     return {
#         "credential_name": cred.name if cred else "Inconnu",
#         "service_url": cred.service_url if cred else None,
#         "username": cred.username if cred else None,
#         "encrypted_payload": shared.encrypted_payload,
#         "decryption_key": shared.share_key,
#         "permission": shared.permission,
#         "message": "Accès autorisé — déchiffrez localement avec le token fourni",
#     }




@router.post("/access")
def access_share(
    req: AccessShareRequest,
    request: Request,
    current_user: User = Depends(get_current_user),  # ✅ obligatoire
    db: Session = Depends(get_db),
):
    """
    ✅ Secure Access (NO SECRET REVEAL)
    - User must be authenticated (ZKP JWT) and email must match recipient_email
    - Token is validated/consumed (zero-trust)
    - We DO NOT return encrypted_payload nor decryption_key anymore
    - Recipient must use /sharing/relay-login to log in without ever seeing the password
    """
    requester_email = (current_user.email or "").strip().lower()
    if not requester_email:
        raise HTTPException(status_code=401, detail="Authenticated user email missing")

    # Validate token (zero-trust) against authenticated email
    share_info = validate_token(req.token, requester_email)
    if not share_info:
        raise HTTPException(status_code=403, detail="Token invalide, expiré, ou email non autorisé")

    # Load share from DB
    token_hash = hashlib.sha256(req.token.encode()).hexdigest()
    shared = db.query(SharedAccess).filter(
        SharedAccess.token_hash == token_hash,
        SharedAccess.is_revoked == False,
    ).first()

    if not shared:
        raise HTTPException(status_code=404, detail="Partage non trouvé")

    if time.time() > shared.expires_at:
        raise HTTPException(status_code=403, detail="Partage expiré")

    # DB-level email check
    if (shared.recipient_email or "").strip().lower() != requester_email:
        raise HTTPException(status_code=403, detail="Email non autorisé pour ce partage")

    # Audit trail (access attempt recorded)
    # shared.use_count += 1
    # shared.used_at = time.time()
    # ip = request.client.host if request.client else "unknown"
    # ua = request.headers.get("user-agent", "unknown")
    # shared.add_access_log_entry(ip, ua)

    # # If one-time, revoke after max uses
    # if shared.permission == "read_once" and shared.use_count >= shared.max_uses:
    #     shared.is_revoked = True
    #     shared.revoked_at = time.time()

    # db.commit()


        # Audit trail (NO consumption here)
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    shared.add_access_log_entry(ip, ua)
    db.commit()





    # Credential metadata only (no secret material)
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
    """Liste les partages actifs créés par l'utilisateur."""
    shares = db.query(SharedAccess).filter(
        SharedAccess.owner_id == current_user.id,
        SharedAccess.is_revoked == False,
    ).all()
    result = []
    for s in shares:
        cred = db.query(Credential).filter(Credential.id == s.credential_id).first()
        result.append({
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
        })
    return result


@router.delete("/revoke/{share_id}")
def revoke_share(
    share_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Révoque un partage (seul le propriétaire peut révoquer)."""
    shared = db.query(SharedAccess).filter(
        SharedAccess.id == share_id,
        SharedAccess.owner_id == current_user.id,
    ).first()
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
    """Retourne le journal d'audit d'un partage (accès, IPs, timestamps)."""
    shared = db.query(SharedAccess).filter(
        SharedAccess.id == share_id,
        SharedAccess.owner_id == current_user.id,
    ).first()
    if not shared:
        raise HTTPException(status_code=404, detail="Partage non trouvé")

    import json
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




# @router.post("/relay-login")
# def relay_login(
#     # req: RelayLoginRequest,
#     # request: Request,
#     # db: Session = Depends(get_db),
#     req: RelayLoginRequest,
#     request: Request,
#     current_user: User = Depends(get_current_user),  # ✅ obligatoire
#     db: Session = Depends(get_db),
# ):
#     """
#     Secure Login Relay:
#     - Recipient provides share token + requester_email
#     - Server validates token (zero-trust) + retrieves encrypted payload
#     - Server decrypts payload using the token (one-time key)
#     - Server performs login to target service using Playwright
#     - Returns cookies (session) to recipient
#     Recipient never sees the password.
#     """
#     # share_info = validate_and_consume_token(req.token, req.requester_email)
#     requester_email = current_user.email
#     share_info = validate_and_consume_token(req.token, requester_email)
#     if not share_info:
#         raise HTTPException(status_code=403, detail="Token invalide, expiré, ou email non autorisé")


   

#     token_hash = hashlib.sha256(req.token.encode()).hexdigest()
#     shared = db.query(SharedAccess).filter(
#         SharedAccess.token_hash == token_hash,
#         SharedAccess.is_revoked == False,
#     ).first()

#     if not shared:
#         raise HTTPException(status_code=404, detail="Partage non trouvé")


#     if (shared.recipient_email or "").lower().strip() != (requester_email or "").lower().strip():
#         raise HTTPException(status_code=403, detail="Email non autorisé pour ce partage")



#     if time.time() > shared.expires_at:
#         raise HTTPException(status_code=403, detail="Partage expiré")

#     # Fetch credential metadata
#     cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()
#     if not cred:
#         raise HTTPException(status_code=404, detail="Credential non trouvé")

#     service_url = req.service_url_override or (cred.service_url or "")
#     if not service_url:
#         raise HTTPException(status_code=400, detail="service_url missing on credential")

#     # decrypt payload (expect it contains JSON with at least "password")
#     try:
#         encrypted_data = json.loads(shared.encrypted_payload)
#     except Exception:
#         raise HTTPException(status_code=400, detail="encrypted_payload must be JSON string {nonce,ciphertext}")

#     try:
#         plaintext = ShareEncryptor.decrypt_from_share(encrypted_data, shared.share_key)
#         payload = json.loads(plaintext) if plaintext.strip().startswith("{") else {"password": plaintext}
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=f"Cannot decrypt share payload: {e}")

#     password = payload.get("password")
#     if not password:
#         raise HTTPException(status_code=400, detail="Share payload missing 'password'")

#     relay_profile = payload.get("relay_profile") or {}  # allow embedding it in plaintext

#     # Extraire le domaine de l'URL pour trouver un profil prédéfini
#     domain = urllib.parse.urlparse(service_url).netloc
#     domain = domain.split(':')[0]  # enlever le port éventuel

#     # Si aucun profil n'est fourni dans le payload, utiliser celui du dictionnaire
#     if not relay_profile:
#         relay_profile = RELAY_PROFILES.get(domain, {})

#     try:
#         # Exécuter le login (asynchrone, on utilise asyncio.run() car la route est synchrone)
#         result = asyncio.run(login_and_get_cookies(
#             service_url=service_url,
#             username=cred.username or req.requester_email,
#             password=password,
#             profile=relay_profile,
#         ))
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=f"Relay login failed: {e}")

#     # Audit + one-time revoke logic
#     shared.use_count += 1
#     shared.used_at = time.time()
#     ip = request.client.host if request.client else "unknown"
#     ua = request.headers.get("user-agent", "unknown")
#     shared.add_access_log_entry(ip, ua)

#     if shared.permission == "read_once" and shared.use_count >= shared.max_uses:
#         shared.is_revoked = True
#         shared.revoked_at = time.time()

#     db.commit()

#     return {
#         "credential_name": cred.name,
#         "service_url": service_url,
#         "username": cred.username,
#         "relay": {
#             "current_url": result.get("current_url"),
#             "title": result.get("title"),
#             "used_selectors": result.get("used_selectors"),
#         },
#         # cookies returned to set in browser/client
#         "cookies": result.get("cookies", []),
#         "localStorage": result.get("localStorage"),  # Ajouté

#         "message": "Relay login done. Recipient never received the password.",
#     }





@router.post("/relay-login")
async def relay_login(
    req: RelayLoginRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Secure Login Relay:
    - Recipient provides share token
    - Server requires requester to be authenticated (JWT) and uses current_user.email
    - Server validates token + retrieves encrypted payload
    - Server decrypts payload using the share_key stored in DB
    - Server performs login to target service using Playwright
    - Returns cookies (session) to recipient
    """
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

    # Extra DB-level check (important if in-memory token store resets)
    shared_recipient = (shared.recipient_email or "").strip().lower()
    if shared_recipient != requester_email:
        raise HTTPException(status_code=403, detail="Email non autorisé pour ce partage")

    # 3) Fetch credential metadata
    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential non trouvé")

    service_url = req.service_url_override or (cred.service_url or "")
    service_url = service_url.strip()
    if not service_url:
        raise HTTPException(status_code=400, detail="service_url missing on credential")



        # Choose relay profile by domain if not provided in payload
    # domain = urllib.parse.urlparse(service_url).netloc.split(":")[0].lower().strip()

        # Choose relay profile by domain if not provided in payload
    domain = urllib.parse.urlparse(service_url).netloc
    domain = domain.split(":")[0].lower().strip()



    # 4) Decrypt payload using share_key stored in DB (NOT req.token)
    try:
        encrypted_data = json.loads(shared.encrypted_payload)
    except Exception:
        raise HTTPException(status_code=400, detail="encrypted_payload must be JSON string {nonce,ciphertext}")

    try:
        plaintext = ShareEncryptor.decrypt_from_share(encrypted_data, shared.share_key)
        plaintext = plaintext or ""
        payload = json.loads(plaintext) if plaintext.strip().startswith("{") else {"password": plaintext}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot decrypt share payload: {e}")

    password = (payload.get("password") or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="Share payload missing 'password'")

    # relay_profile = payload.get("relay_profile") or {}


    relay_profile = payload.get("relay_profile") or {}
    if not relay_profile:
        relay_profile = RELAY_PROFILES.get(domain, {})



    # 5) Perform login via Playwright
    # IMPORTANT: login_and_get_cookies must be async for this to work properly.
    # try:
    #     result = await login_and_get_cookies(
    #         service_url=service_url,
    #         username=(cred.username or requester_email).strip(),
    #         password=password,
    #         profile=relay_profile,
    #     )
    # except Exception as e:
    #     raise HTTPException(status_code=400, detail=f"Relay login failed: {e}")


    try:
        result = await asyncio.wait_for(
            login_and_get_cookies(
                service_url=service_url,
                username=(cred.username or requester_email).strip(),
                password=password,
                profile=relay_profile,
            ),
            
            timeout=90,  # seconds
        )
        cookies = result.get("cookies", [])
        local_storage = result.get("localStorage")   # peut être None

        session_id = _handoff_store_put(service_url, cookies, local_storage)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Relay login timed out (Playwright took too long)")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Relay login failed: {e}")





    # Ensure result is a dict (avoid .get errors)
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Relay login returned invalid result type")

    # 6) Audit + revoke logic
    shared.use_count += 1
    shared.used_at = time.time()
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    shared.add_access_log_entry(ip, ua)

    if shared.permission == "read_once" and shared.use_count >= shared.max_uses:
        shared.is_revoked = True
        shared.revoked_at = time.time()

    db.commit()

    # return {
    #     "credential_name": cred.name,
    #     "service_url": service_url,
    #     "username": cred.username,
    #     "relay": {
    #         "current_url": result.get("current_url"),
    #         "title": result.get("title"),
    #         "used_selectors": result.get("used_selectors"),
    #     },
    #     "cookies": result.get("cookies", []),
    #     "message": "Relay login done. Recipient never received the password.",
    # }


        # Create a short-lived handoff session for browser extension
    cookies = result.get("cookies", [])
    # session_id = _handoff_store_put(service_url=service_url, cookies=cookies)
    session_id = _handoff_store_put(service_url=service_url, cookies=cookies, current_url=result.get("current_url"))


    
    domain = urllib.parse.urlparse(service_url).netloc.split(':')[0]
    # Normaliser : enlever le préfixe www.
    if domain.startswith('www.'):
        domain = domain[4:]


    relay_profile = RELAY_PROFILES.get(domain, {})
    print(f"DEBUG: domain={domain}, profile={relay_profile}")
    return {
        "credential_name": cred.name,
        "service_url": service_url,
        "username": cred.username,
        "relay": {
            "current_url": result.get("current_url"),
            "title": result.get("title"),
            "used_selectors": result.get("used_selectors"),
            "login_detected": result.get("login_detected", False),  # ✅ NEW

        },
        "handoff": {
            "session_id": session_id,
            "expires_in": COOKIE_HANDOFF_TTL_SECONDS,
        },
        "message": "Relay login done. Cookies stored server-side for extension handoff (not shown to frontend).",
    }






@router.get("/handoff/{session_id}")
def get_handoff(session_id: str):
    """
    Used by the browser extension to fetch cookies and target URL.
    Session is short-lived and ONE-TIME (consumed).
    """
    data = _handoff_store_get(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Handoff session not found or expired")

    return {
        "service_url": data.get("service_url"),

        "current_url": data.get("current_url"),

        "cookies": data.get("cookies", []),

        "localStorage": data.get("localStorage"),  # Ajouté
    }


















@router.post("/create-intent", response_model=ShareIntentResponse)
def create_share_intent(
    req: ShareIntentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Vérification propriétaire
    cred = db.query(Credential).filter(
        Credential.id == req.credential_id,
        Credential.owner_id == current_user.id,
        Credential.is_active == True,
    ).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential non trouvé")

    # 1) Génère le token de partage (servira aussi de clé de chiffrement côté client)
    share_token = generate_share_token(
        credential_id=req.credential_id,
        owner_id=current_user.id,
        recipient_email=req.recipient_email,
        permission=req.permission,
        ttl_hours=req.ttl_hours,
        max_uses=req.max_uses,
    )
    token_hash = hashlib.sha256(share_token.encode()).hexdigest()

    # 2) Crée l'entrée DB mais sans payload pour l’instant
    shared = SharedAccess(
        credential_id=req.credential_id,
        owner_id=current_user.id,
        recipient_email=req.recipient_email,
        token_hash=token_hash,
        encrypted_payload="{}",  # placeholder (sera remplacé dans /finalize)
        share_key=share_token,   # ✅ IMPORTANT: satisfy NOT NULL + same key for decrypt
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
    """
    Attache le encrypted_payload (déjà chiffré côté client avec le share_token) au share.
    """
    token_hash = hashlib.sha256(req.token.encode()).hexdigest()

    shared = db.query(SharedAccess).filter(
        SharedAccess.token_hash == token_hash,
        SharedAccess.owner_id == current_user.id,
        SharedAccess.is_revoked == False,
    ).first()

    if not shared:
        raise HTTPException(status_code=404, detail="Partage non trouvé (ou déjà révoqué)")

    if time.time() > shared.expires_at:
        raise HTTPException(status_code=403, detail="Partage expiré")

    # Validate payload format early
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




@router.get("/handoff/{session_id}")
def handoff_get(session_id: str):
    """
    One-time cookie handoff endpoint for a browser extension.
    - short-lived (TTL)
    - one-time consume
    """
    data = _handoff_store_consume(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Handoff session not found or expired")

    return {
        "service_url": data.get("service_url"),
        "cookies": data.get("cookies", []),
    }










# def _handoff_store_put(service_url: str, cookies: list) -> str:
#     session_id = secrets.token_urlsafe(24)
#     now = time.time()
#     with COOKIE_HANDOFF_LOCK:
#         # cleanup
#         for sid, v in list(COOKIE_HANDOFF_STORE.items()):
#             if now - v.get("created_at", now) > COOKIE_HANDOFF_TTL_SECONDS:
#                 COOKIE_HANDOFF_STORE.pop(sid, None)

#         COOKIE_HANDOFF_STORE[session_id] = {
#             "service_url": service_url,
#             "cookies": cookies,
#             "created_at": now,
#         }
#     return session_id


# def _handoff_store_put(service_url: str, cookies: list, current_url: str | None = None) -> str:
#     session_id = secrets.token_urlsafe(24)
#     now = time.time()
#     with COOKIE_HANDOFF_LOCK:
#         _handoff_cleanup(now)
#         COOKIE_HANDOFF_STORE[session_id] = {
#             "service_url": service_url,
#             "current_url": current_url or service_url,
#             "cookies": cookies,
#             "created_at": now,
#         }
#     return session_id



# def _handoff_store_get(session_id: str) -> dict | None:
#     now = time.time()
#     with COOKIE_HANDOFF_LOCK:
#         v = COOKIE_HANDOFF_STORE.get(session_id)
#         if not v:
#             return None
#         if now - v.get("created_at", now) > COOKIE_HANDOFF_TTL_SECONDS:
#             # COOKIE_HANDOFF_STORE.pop(session_id, None)
#             return None
#         return v





def _handoff_store_put(service_url: str, cookies: list, local_storage: dict = None) -> str:
    session_id = secrets.token_urlsafe(24)
    now = time.time()
    with COOKIE_HANDOFF_LOCK:
        # nettoyage des sessions expirées
        for sid, v in list(COOKIE_HANDOFF_STORE.items()):
            if now - v.get("created_at", now) > COOKIE_HANDOFF_TTL_SECONDS:
                COOKIE_HANDOFF_STORE.pop(sid, None)
        COOKIE_HANDOFF_STORE[session_id] = {
            "service_url": service_url,
            "cookies": cookies,
            "local_storage": local_storage,
            "created_at": now,
        }
    return session_id

def _handoff_store_get(session_id: str) -> dict | None:
    now = time.time()
    with COOKIE_HANDOFF_LOCK:
        v = COOKIE_HANDOFF_STORE.get(session_id)
        if not v:
            return None
        if now - v.get("created_at", now) > COOKIE_HANDOFF_TTL_SECONDS:
            COOKIE_HANDOFF_STORE.pop(session_id, None)
            return None
        return v   # ne supprime pas



















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