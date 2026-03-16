"""
Database Models - SQLAlchemy + SQLite
=====================================
Modèles de données pour le système ZKP de partage de credentials.
Architecture Zero-Trust : les données sensibles sont TOUJOURS chiffrées.
"""

import json
import os
import time
from typing import Optional

from sqlalchemy import (
    Boolean, Column, Float, ForeignKey, Integer, String, Text, create_engine, event
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./zkp_credentials.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=False,
)

# WAL mode pour SQLite (meilleures performances concurrentes)
if "sqlite" in DATABASE_URL:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# ─── Modèles ──────────────────────────────────────────────────────────────────

class User(Base):
    """
    Utilisateur du système.
    - Le mot de passe n'est JAMAIS stocké.
    - Seule la clé publique ZKP (Y = g^x mod p) est stockée.
    - Le salt ZKP est stocké pour permettre la re-dérivation du secret.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    username = Column(String(100), unique=True, nullable=False)

    # ZKP : clé publique Y = g^x mod p (jamais le mot de passe)
    zkp_public_key = Column(Text, nullable=False)   # Stocké en hex
    zkp_salt = Column(String(128), nullable=False)  # Salt pour dériver x

    # Chiffrement des credentials : salt pour la clé maître AES
    master_salt = Column(String(128), nullable=False)

    # Zero-Trust metadata
    created_at = Column(Float, default=time.time)
    last_login = Column(Float, nullable=True)
    is_active = Column(Boolean, default=True)
    failed_attempts = Column(Integer, default=0)
    locked_until = Column(Float, nullable=True)     # Brute-force protection

    credentials = relationship("Credential", back_populates="owner", cascade="all, delete-orphan")
    issued_shares = relationship("SharedAccess", back_populates="owner", foreign_keys="SharedAccess.owner_id")


class Credential(Base):
    """
    Credential chiffré (mot de passe, clé API, token, etc.).
    - Le secret est chiffré avec AES-256-GCM.
    - La clé AES est dérivée du mot de passe master (PBKDF2).
    - Principe du moindre privilège : chaque credential a ses propres métadonnées.
    """
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False)              # ex: "DVWA Admin"
    service_url = Column(String(500), nullable=True)        # ex: "http://dvwa:80"
    username = Column(String(255), nullable=True)           # login (clair, pas sensible)
    credential_type = Column(String(50), default="password")  # password|api_key|token|certificate

    # Données chiffrées (AES-256-GCM)
    encrypted_secret = Column(Text, nullable=False)         # JSON: {salt, nonce, ciphertext}

    # Métadonnées Zero-Trust
    created_at = Column(Float, default=time.time)
    updated_at = Column(Float, default=time.time)
    last_used = Column(Float, nullable=True)
    tags = Column(String(500), default="")                   # CSV tags
    notes = Column(Text, nullable=True)                      # Notes chiffrées (optionnel)
    is_active = Column(Boolean, default=True)

    owner = relationship("User", back_populates="credentials")
    shares = relationship("SharedAccess", back_populates="credential", cascade="all, delete-orphan")


class SharedAccess(Base):
    """
    Enregistrement d'un partage de credential.
    - Le secret n'est JAMAIS stocké en clair.
    - Le partage utilise un token éphémère one-time.
    - Audit trail complet.
    """
    __tablename__ = "shared_accesses"


    share_key = Column(String, nullable=False)   # stocke la clé éphémère

    id = Column(Integer, primary_key=True, index=True)
    credential_id = Column(Integer, ForeignKey("credentials.id"), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    recipient_email = Column(String(255), nullable=False)

    # Token de partage (seul le hash est stocké)
    token_hash = Column(String(64), unique=True, nullable=False)

    # Données partagées chiffrées avec la clé éphémère
    encrypted_payload = Column(Text, nullable=False)        # JSON chiffré one-time key

    # Contrôle d'accès
    permission = Column(String(50), default="read_once")    # read | read_once
    max_uses = Column(Integer, default=1)
    use_count = Column(Integer, default=0)
    expires_at = Column(Float, nullable=False)
    used_at = Column(Float, nullable=True)

    # Audit
    created_at = Column(Float, default=time.time)
    is_revoked = Column(Boolean, default=False)
    revoked_at = Column(Float, nullable=True)
    access_log = Column(Text, default="[]")                 # JSON list d'accès

    owner = relationship("User", back_populates="issued_shares", foreign_keys=[owner_id])
    credential = relationship("Credential", back_populates="shares")

    def add_access_log_entry(self, ip: str, user_agent: str):
        """Ajoute une entrée d'audit."""
        log = json.loads(self.access_log or "[]")
        log.append({"ts": time.time(), "ip": ip, "ua": user_agent})
        self.access_log = json.dumps(log)


class ZKPChallenge(Base):
    """
    Challenge ZKP temporaire pour le protocole d'authentification.
    Durée de vie courte (5 minutes) pour éviter les replay attacks.
    """
    __tablename__ = "zkp_challenges"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String(255), nullable=False, index=True)
    challenge_value = Column(Text, nullable=False)           # Valeur du challenge (int)
    commitment_value = Column(Text, nullable=False)          # Engagement du prouveur
    expires_at = Column(Float, nullable=False)
    used = Column(Boolean, default=False)
    created_at = Column(Float, default=time.time)


def get_db():
    """Dependency injection FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    """Crée toutes les tables."""
    Base.metadata.create_all(bind=engine)