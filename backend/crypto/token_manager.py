"""
Secure Token Manager - One-Time Share Tokens
=============================================
Gestion des tokens de partage avec:
- Expiration temporelle
- Usage unique (one-time use)
- Lien vers les permissions (read-only, time-limited)
- Architecture Zero-Trust : chaque accès est vérifié
"""

import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Dict, Optional


@dataclass
class ShareToken:
    """Représente un token de partage sécurisé."""
    token_hash: str           # SHA-256 du token (jamais le token brut)
    credential_id: int        # ID de la credential partagée
    owner_id: int             # ID du propriétaire
    recipient_email: str      # Email du destinataire autorisé
    permission: str           # "read" | "read_once"
    expires_at: float         # Timestamp UNIX d'expiration
    used: bool = False        # Token déjà utilisé ?
    max_uses: int = 1         # Nombre max d'utilisations
    use_count: int = 0        # Nombre d'utilisations actuelles
    created_at: float = 0.0

    def __post_init__(self):
        if self.created_at == 0.0:
            self.created_at = time.time()

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def is_exhausted(self) -> bool:
        return self.use_count >= self.max_uses

    @property
    def is_valid(self) -> bool:
        return not self.is_expired and not self.is_exhausted and not self.used


# ─── Store en mémoire (remplacer par Redis en production) ──────────────────
_TOKEN_STORE: Dict[str, Dict] = {}


def _hash_token(token: str) -> str:
    """Hash un token avec SHA-256 pour le stockage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_share_token(
    credential_id: int,
    owner_id: int,
    recipient_email: str,
    permission: str = "read_once",
    ttl_hours: int = 24,
    max_uses: int = 1,
) -> str:
    """
    Génère un token de partage sécurisé (256 bits d'entropie).
    Stocke le hash du token (jamais le token brut).
    Retourne le token en clair (à envoyer au destinataire).
    """
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    share = ShareToken(
        token_hash=token_hash,
        credential_id=credential_id,
        owner_id=owner_id,
        recipient_email=recipient_email,
        permission=permission,
        expires_at=time.time() + ttl_hours * 3600,
        max_uses=max_uses,
    )
    _TOKEN_STORE[token_hash] = asdict(share)
    return token


def validate_and_consume_token(token: str, requester_email: str) -> Optional[ShareToken]:
    """
    Valide un token de partage :
    1. Vérifie que le token existe
    2. Vérifie que le destinataire correspond
    3. Vérifie la non-expiration
    4. Consomme le token (incrémente use_count)
    Retourne le ShareToken si valide, None sinon.
    """
    token_hash = _hash_token(token)
    record = _TOKEN_STORE.get(token_hash)
    if not record:
        return None

    share = ShareToken(**record)

    # Zero-Trust : vérification stricte du destinataire
    if share.recipient_email.lower() != requester_email.lower():
        return None

    if not share.is_valid:
        # Nettoyage si expiré ou épuisé
        if share.is_expired or share.is_exhausted:
            del _TOKEN_STORE[token_hash]
        return None

    # Consommation du token
    share.use_count += 1
    if share.use_count >= share.max_uses:
        share.used = True

    _TOKEN_STORE[token_hash] = asdict(share)
    return share


def validate_token(token: str, requester_email: str) -> Optional[ShareToken]:
    """
    Validate token WITHOUT consuming it:
    - exists
    - recipient email matches
    - not expired / not exhausted / not used
    Returns ShareToken if valid, None otherwise.
    """
    token_hash = _hash_token(token)
    record = _TOKEN_STORE.get(token_hash)
    if not record:
        return None

    share = ShareToken(**record)

    if share.recipient_email.lower() != requester_email.lower():
        return None

    if not share.is_valid:
        if share.is_expired or share.is_exhausted:
            del _TOKEN_STORE[token_hash]
        return None

    return share







def revoke_token(token: str, owner_id: int) -> bool:
    """Révoque un token (seul le propriétaire peut révoquer)."""
    token_hash = _hash_token(token)
    record = _TOKEN_STORE.get(token_hash)
    if not record:
        return False
    if record["owner_id"] != owner_id:
        return False
    del _TOKEN_STORE[token_hash]
    return True


def list_active_tokens(owner_id: int) -> list:
    """Liste les tokens actifs d'un propriétaire."""
    active = []
    for token_hash, record in list(_TOKEN_STORE.items()):
        share = ShareToken(**record)
        if share.owner_id == owner_id:
            if share.is_expired or share.is_exhausted:
                del _TOKEN_STORE[token_hash]
                continue
            active.append({
                "token_hash_preview": token_hash[:8] + "...",
                "credential_id": share.credential_id,
                "recipient_email": share.recipient_email,
                "permission": share.permission,
                "expires_at": share.expires_at,
                "use_count": share.use_count,
                "max_uses": share.max_uses,
            })
    return active


def cleanup_expired_tokens():
    """Supprime tous les tokens expirés (à appeler périodiquement)."""
    expired = [h for h, r in _TOKEN_STORE.items() if ShareToken(**r).is_expired]
    for h in expired:
        del _TOKEN_STORE[h]