"""
Auth Router - Zero-Knowledge Proof Authentication
==================================================
Endpoints d'authentification ZKP (Schnorr Protocol).

Flux d'inscription :
  POST /auth/register  → Enregistre email + clé publique ZKP (jamais le password)

Flux de connexion (3 étapes) :
  POST /auth/challenge  → Serveur génère un challenge
  POST /auth/verify     → Client envoie la preuve ZKP
  → JWT retourné si preuve valide
"""

import base64
import json
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from backend.crypto.zkp_engine import SchnorrZKP, ZKPProof, ZKPPublicKey
from backend.models.database import User, ZKPChallenge, get_db

router = APIRouter(prefix="/auth", tags=["Authentication ZKP"])

JWT_SECRET = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION_USE_256BIT_KEY")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 8
CHALLENGE_TTL = 300  # 5 minutes

zkp_engine = SchnorrZKP()
security = HTTPBearer()


# ─── Schemas ──────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: str
    username: str
    zkp_public_key: str      # Y = g^x mod p en hex
    zkp_salt: str            # Salt pour dériver x (base64)
    master_salt: str         # Salt pour clé AES maître (base64)


class ChallengeRequest(BaseModel):
    email: str
    commitment: str          # Y_r = g^r mod p en hex (étape 1 du prouveur)


class VerifyRequest(BaseModel):
    email: str
    challenge_id: int
    response: str            # s = r - c*x mod q en hex (réponse du prouveur)


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: int
    username: str


class ChallengeResponse(BaseModel):
    challenge_id: int
    challenge_value: str     # c en hex (envoyé au client)
    expires_at: float


# ─── Helpers ──────────────────────────────────────────────────────────────────

def create_jwt(user_id: int, email: str, username: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "username": username,
        "exp": time.time() + JWT_EXPIRE_HOURS * 3600,
        "iat": time.time(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalide ou expiré")


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    payload = decode_jwt(credentials.credentials)
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Utilisateur non trouvé ou inactif")
    return user


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/register", status_code=201)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    """
    Inscription Zero-Knowledge :
    - Seule la clé publique ZKP est stockée (jamais le mot de passe).
    - Le salt AES est stocké pour permettre la dérivation de la clé de chiffrement.
    """
    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(status_code=409, detail="Email déjà utilisé")
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(status_code=409, detail="Username déjà utilisé")

    user = User(
        email=req.email,
        username=req.username,
        zkp_public_key=req.zkp_public_key,
        zkp_salt=req.zkp_salt,
        master_salt=req.master_salt,
        created_at=time.time(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"message": "Inscription réussie (Zero-Knowledge)", "user_id": user.id}


@router.post("/challenge", response_model=ChallengeResponse)
def get_challenge(req: ChallengeRequest, db: Session = Depends(get_db)):
    """
    Étape 1 du protocole ZKP :
    Le client envoie son engagement Y_r = g^r mod p.
    Le serveur génère un challenge c = H(Y || Y_r || context).
    """
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

    # Brute-force protection
    if user.locked_until and time.time() < user.locked_until:
        raise HTTPException(status_code=429, detail="Compte temporairement bloqué")

    commitment_int = int(req.commitment, 16)
    public_key_int = int(user.zkp_public_key, 16)

    challenge_val = zkp_engine.generate_challenge(
        public_key_int, commitment_int, context=req.email
    )

    # Stocke le challenge avec TTL (anti-replay)
    db_challenge = ZKPChallenge(
        user_email=req.email,
        challenge_value=hex(challenge_val),
        commitment_value=req.commitment,
        expires_at=time.time() + CHALLENGE_TTL,
    )
    db.add(db_challenge)
    db.commit()
    db.refresh(db_challenge)

    return ChallengeResponse(
        challenge_id=db_challenge.id,
        challenge_value=hex(challenge_val),
        expires_at=db_challenge.expires_at,
    )


@router.post("/verify", response_model=AuthResponse)
def verify_proof(req: VerifyRequest, db: Session = Depends(get_db)):
    """
    Étape 2 du protocole ZKP :
    Le client envoie sa réponse s = r - c*x mod q.
    Le serveur vérifie : g^s * Y^c ≡ Y_r (mod p).
    Aucun mot de passe ne transite — Zero-Knowledge prouvé.
    """
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

    # Récupère le challenge
    db_challenge = db.query(ZKPChallenge).filter(
        ZKPChallenge.id == req.challenge_id,
        ZKPChallenge.user_email == req.email,
        ZKPChallenge.used == False,
    ).first()
    if not db_challenge:
        raise HTTPException(status_code=400, detail="Challenge introuvable ou déjà utilisé")
    if time.time() > db_challenge.expires_at:
        db.delete(db_challenge)
        db.commit()
        raise HTTPException(status_code=400, detail="Challenge expiré")

    # Construction de la preuve
    commitment_int = int(db_challenge.commitment_value, 16)
    challenge_int = int(db_challenge.challenge_value, 16)
    response_int = int(req.response, 16)
    public_key_int = int(user.zkp_public_key, 16)

    # Vérification ZKP
    valid = zkp_engine.interactive_verify(
        commitment_value=commitment_int,
        public_key_value=public_key_int,
        response=response_int,
        challenge=challenge_int,
    )

    # Marque le challenge comme utilisé (anti-replay)
    db_challenge.used = True
    db.commit()

    if not valid:
        user.failed_attempts += 1
        if user.failed_attempts >= 5:
            user.locked_until = time.time() + 900  # 15 minutes de blocage
        db.commit()
        raise HTTPException(status_code=401, detail="Preuve ZKP invalide")

    # Succès
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login = time.time()
    db.commit()

    token = create_jwt(user.id, user.email, user.username)
    return AuthResponse(
        access_token=token,
        expires_in=JWT_EXPIRE_HOURS * 3600,
        user_id=user.id,
        username=user.username,
    )


@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    """Retourne les infos de l'utilisateur connecté (sans données sensibles)."""
    return {
        "id": current_user.id,
        "email": current_user.email,
        "username": current_user.username,
        "created_at": current_user.created_at,
        "last_login": current_user.last_login,
    }


@router.get("/salts/{email}")
def get_salts(email: str, db: Session = Depends(get_db)):
    """
    Retourne les salts publics nécessaires au client pour dériver ses clés.
    (Le salt ZKP pour dériver x, le salt AES pour dériver la clé de chiffrement)
    Ces informations sont publiques dans le protocole ZKP.
    """
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    return {
        "zkp_salt": user.zkp_salt,
        "master_salt": user.master_salt,
    }