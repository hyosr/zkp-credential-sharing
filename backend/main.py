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

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

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

from backend.routers.final_capture_share import router as final_capture_share_router

from backend.routers.owner_handoff import router as owner_handoff_router



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




class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response: Response = await call_next(request)
        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Prevent clickjacking (optional but recommended)
        response.headers["X-Frame-Options"] = "DENY"
        # Enable browser XSS filtering (deprecated but harmless)
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Strict Transport Security (only for HTTPS – uncomment if you use HTTPS)
        # response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


# Add the middleware to the app
app.add_middleware(SecurityHeadersMiddleware)




# ─── Middleware ────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8502", "http://localhost:3000", "chrome-extension://*","http://localhost:8501", "http://127.0.0.1:8501", ],
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

app.include_router(final_capture_share_router)

app.include_router(owner_handoff_router)


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



















