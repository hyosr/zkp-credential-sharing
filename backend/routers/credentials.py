"""
Credentials Router - CRUD chiffré
===================================
Gestion des credentials chiffrés côté serveur.
Les secrets sont TOUJOURS chiffrés avant persistance.
Le serveur ne connaît JAMAIS les mots de passe en clair.
"""

import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.crypto.encryption import CredentialEncryptor
from backend.models.database import Credential, get_db
from backend.routers.auth import get_current_user
from backend.models.database import User

router = APIRouter(prefix="/credentials", tags=["Credentials"])


# ─── Schemas ──────────────────────────────────────────────────────────────────

class CredentialCreate(BaseModel):
    name: str
    service_url: Optional[str] = None
    username: Optional[str] = None
    credential_type: str = "password"
    encrypted_secret: str   # Le client envoie le secret déjà chiffré côté client
    tags: Optional[str] = ""
    notes: Optional[str] = None


class CredentialUpdate(BaseModel):
    name: Optional[str] = None
    service_url: Optional[str] = None
    username: Optional[str] = None
    encrypted_secret: Optional[str] = None
    tags: Optional[str] = None
    notes: Optional[str] = None


class CredentialOut(BaseModel):
    id: int
    name: str
    service_url: Optional[str]
    username: Optional[str]
    credential_type: str
    tags: str
    notes: Optional[str]
    created_at: float
    updated_at: float
    last_used: Optional[float]
    shares_count: int = 0

    class Config:
        from_attributes = True


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/", status_code=201)
def create_credential(
    req: CredentialCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Crée un credential.
    Le secret est chiffré côté client (double chiffrement :
    client-side AES + server-side storage).
    """
    cred = Credential(
        owner_id=current_user.id,
        name=req.name,
        service_url=req.service_url,
        username=req.username,
        credential_type=req.credential_type,
        encrypted_secret=req.encrypted_secret,
        tags=req.tags or "",
        notes=req.notes,
        created_at=time.time(),
        updated_at=time.time(),
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return {"message": "Credential créé", "id": cred.id}


@router.get("/", response_model=List[CredentialOut])
def list_credentials(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Liste les credentials de l'utilisateur (sans les secrets)."""
    creds = db.query(Credential).filter(
        Credential.owner_id == current_user.id,
        Credential.is_active == True,
    ).all()
    result = []
    for c in creds:
        result.append(CredentialOut(
            id=c.id,
            name=c.name,
            service_url=c.service_url,
            username=c.username,
            credential_type=c.credential_type,
            tags=c.tags,
            notes=None,  # Notes non retournées dans la liste
            created_at=c.created_at,
            updated_at=c.updated_at,
            last_used=c.last_used,
            shares_count=len(c.shares),
        ))
    return result


@router.get("/{cred_id}/encrypted")
def get_encrypted_credential(
    cred_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retourne le blob chiffré d'un credential.
    Le client le déchiffrera localement avec son mot de passe master.
    Zero-Knowledge : le serveur ne peut pas lire le secret.
    """
    cred = db.query(Credential).filter(
        Credential.id == cred_id,
        Credential.owner_id == current_user.id,
        Credential.is_active == True,
    ).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential non trouvé")
    cred.last_used = time.time()
    db.commit()
    return {
        "id": cred.id,
        "name": cred.name,
        "username": cred.username,
        "service_url": cred.service_url,
        "encrypted_secret": cred.encrypted_secret,  # Blob chiffré
        "master_salt": current_user.master_salt,     # Salt pour dériver la clé AES
    }


@router.put("/{cred_id}")
def update_credential(
    cred_id: int,
    req: CredentialUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Met à jour un credential."""
    cred = db.query(Credential).filter(
        Credential.id == cred_id,
        Credential.owner_id == current_user.id,
    ).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential non trouvé")
    if req.name:
        cred.name = req.name
    if req.service_url is not None:
        cred.service_url = req.service_url
    if req.username is not None:
        cred.username = req.username
    if req.encrypted_secret is not None:
        cred.encrypted_secret = req.encrypted_secret
    if req.tags is not None:
        cred.tags = req.tags
    if req.notes is not None:
        cred.notes = req.notes
    cred.updated_at = time.time()
    db.commit()
    return {"message": "Credential mis à jour"}


@router.delete("/{cred_id}")
def delete_credential(
    cred_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Supprime (soft-delete) un credential."""
    cred = db.query(Credential).filter(
        Credential.id == cred_id,
        Credential.owner_id == current_user.id,
    ).first()
    if not cred:
        raise HTTPException(status_code=404, detail="Credential non trouvé")
    cred.is_active = False
    cred.updated_at = time.time()
    db.commit()
    return {"message": "Credential supprimé"}