import json
import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.auth.keycloak_auth import get_keycloak_user
from backend.crypto.encryption import ShareEncryptor
from backend.models.database import SharedAccess, Credential, get_db

router = APIRouter(prefix="/keycloak", tags=["Keycloak Passwordless Share"])


@router.get("/secret/{share_id}")
def get_secret_via_keycloak(
    share_id: int,
    request: Request,
    claims: Dict[str, Any] = Depends(get_keycloak_user),
    db: Session = Depends(get_db),
):
    # 1) Keycloak token is already verified by get_keycloak_user() (JWKS + exp)
    email = (claims.get("email") or "").strip().lower()
    preferred_username = (claims.get("preferred_username") or "").strip().lower()

    # 2) Load share
    shared = db.query(SharedAccess).filter(SharedAccess.id == share_id).first()
    if not shared:
        raise HTTPException(status_code=404, detail="Share not found")

    if shared.is_revoked:
        raise HTTPException(status_code=403, detail="Share revoked")

    if time.time() > (shared.expires_at or 0):
        raise HTTPException(status_code=403, detail="Share expired")

    # Optional strong check: recipient must match Keycloak token email
    # Uncomment if your Keycloak token always contains email:
    # if email and (shared.recipient_email or "").strip().lower() != email:
    #     raise HTTPException(status_code=403, detail="Not authorized for this share (recipient mismatch)")

    # 3) Decrypt the shared payload (this is the "real secret" in your ZK sharing design)
    try:
        encrypted_data = json.loads(shared.encrypted_payload or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid encrypted_payload format in DB")

    try:
        plaintext = ShareEncryptor.decrypt_from_share(encrypted_data, shared.share_key) or ""
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot decrypt shared payload: {e}")

    secret = plaintext
    try:
        if plaintext.strip().startswith("{"):
            d = json.loads(plaintext)
            secret = d.get("password") or d.get("secret") or plaintext
    except Exception:
        pass

    cred = db.query(Credential).filter(Credential.id == shared.credential_id).first()
    ip = request.client.host if request.client else "unknown"

    return {
        "share_id": share_id,
        "credential_id": shared.credential_id,
        "credential_name": cred.name if cred else None,
        "service_url": cred.service_url if cred else None,
        "username": cred.username if cred else None,
        "secret": secret,
        "whoami": {"email": email, "preferred_username": preferred_username, "ip": ip},
        "message": "OK — Keycloak token validated. Secret returned.",
    }