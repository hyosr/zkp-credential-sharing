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

import hashlib
import json
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.crypto.encryption import ShareEncryptor
from backend.crypto.token_manager import (
    generate_share_token,
    list_active_tokens,
    revoke_token,
    validate_and_consume_token,
)
from backend.models.database import Credential, SharedAccess, User, get_db
from backend.routers.auth import get_current_user

router = APIRouter(prefix="/sharing", tags=["Secure Sharing"])

encryptor = ShareEncryptor()


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
    requester_email: str


class RevokeRequest(BaseModel):
    token_hash_preview: str             # Premier 8 chars du hash (depuis list_tokens)


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


@router.post("/access")
def access_share(
    req: AccessShareRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Accès à un credential partagé via token one-time.
    Zero-Trust : vérifie l'email du demandeur à chaque accès.
    Retourne le blob chiffré (le destinataire le déchiffre avec le token).
    """
    # Validation Zero-Trust du token
    share_info = validate_and_consume_token(req.token, req.requester_email)
    if not share_info:
        raise HTTPException(
            status_code=403,
            detail="Token invalide, expiré, ou email non autorisé"
        )

    # Récupère le partage en base
    token_hash = hashlib.sha256(req.token.encode()).hexdigest()
    shared = db.query(SharedAccess).filter(
        SharedAccess.token_hash == token_hash,
        SharedAccess.is_revoked == False,
    ).first()
    if not shared:
        raise HTTPException(status_code=404, detail="Partage non trouvé")
    if time.time() > shared.expires_at:
        raise HTTPException(status_code=403, detail="Partage expiré")

    # Audit trail
    shared.use_count += 1
    shared.used_at = time.time()
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    shared.add_access_log_entry(ip, ua)

    # Si one-time, invalide après usage
    if shared.permission == "read_once" and shared.use_count >= shared.max_uses:
        shared.is_revoked = True
        shared.revoked_at = time.time()

    db.commit()

    # Récupère infos du credential (sans le secret original)
    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()

    return {
        "credential_name": cred.name if cred else "Inconnu",
        "service_url": cred.service_url if cred else None,
        "username": cred.username if cred else None,
        "encrypted_payload": shared.encrypted_payload,
        # Le destinataire déchiffre avec son token (clé éphémère)
        "decryption_key": req.token,
        "permission": shared.permission,
        "message": "Accès autorisé — déchiffrez localement avec le token fourni",
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