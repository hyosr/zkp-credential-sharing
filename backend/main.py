"""
ZKP Secure Credential Sharing - FastAPI Backend
================================================
Partie 2 du projet AI-Powered Pentest :
Zero-Knowledge Proof Authentication + Secure Credential Sharing
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from backend.models.database import create_tables
from backend.routers import auth, credentials, sharing
from backend.routers import keycloak_sharing  # <-- add

from fastapi import FastAPI, Depends, HTTPException, Header
import jwt
import requests

from backend.routers.extension_bridge import router as extension_bridge_router

from backend.routers.keycloak_secret import router as keycloak_secret_router

from backend.routers.keycloak_handoff import router as keycloak_handoff_router

from backend.models.assisted_access import AssistedAccessRequest  # noqa: F401

from backend.routers.assisted_access import router as assisted_access_router


from backend.routers.handoff import router as handoff_router


from backend.routers.session import router as session_router

from backend.routers.assisted_access import router as assisted_access_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialisation au démarrage."""
    create_tables()
    yield


app = FastAPI(
    title="ZKP Secure Credential Sharing API",
    description="""
    ## Partie 2 : Zero-Knowledge Secure Credential Sharing
    
    Système de partage de credentials basé sur les preuves à divulgation nulle (ZKP).
    
    ### Fonctionnalités :
    - 🔐 **Authentification ZKP** (protocole Schnorr) — aucun mot de passe ne transite
    - 🔒 **Stockage chiffré** AES-256-GCM avec PBKDF2
    - 🤝 **Partage sécurisé** via tokens one-time éphémères
    - 🛡️ **Zero-Trust Architecture** — vérification à chaque accès
    - 📋 **Audit Trail** complet de tous les accès
    - 🚫 **Révocation** instantanée des partages
    """,
    version="2.0.0",
    lifespan=lifespan,
)

# ─── Middleware ────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8502", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Routers ─────────────��────────────────────────────────────────────────────

app.include_router(auth.router)
app.include_router(credentials.router)
app.include_router(sharing.router)
app.include_router(keycloak_sharing.router)  # <-- add
app.include_router(extension_bridge_router)

app.include_router(keycloak_secret_router)

app.include_router(keycloak_handoff_router)


app.include_router(assisted_access_router)

app.include_router(handoff_router)

app.include_router(session_router)

app.include_router(assisted_access_router)


@app.get("/")
def root():
    return {
        "service": "ZKP Secure Credential Sharing",
        "version": "2.0.0",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "healthy", "zkp": "enabled", "encryption": "AES-256-GCM"}



















