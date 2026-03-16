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

