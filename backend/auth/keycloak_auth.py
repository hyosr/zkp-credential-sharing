import os
from typing import Optional, Dict, Any

import requests
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError

security = HTTPBearer()

KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8080").rstrip("/")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "zkp-realm")

# Cache JWKS in-memory (simple, enough for dev)
_JWKS: Optional[Dict[str, Any]] = None


def _get_jwks() -> Dict[str, Any]:
    global _JWKS
    if _JWKS is not None:
        return _JWKS

    jwks_url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs"
    r = requests.get(jwks_url, timeout=10)
    r.raise_for_status()
    _JWKS = r.json()
    return _JWKS


def _get_signing_key(token: str) -> Dict[str, Any]:
    jwks = _get_jwks()
    headers = jwt.get_unverified_header(token)
    kid = headers.get("kid")
    for k in jwks.get("keys", []):
        if k.get("kid") == kid:
            return k
    raise HTTPException(status_code=401, detail="Invalid Keycloak token (unknown kid)")


def verify_keycloak_token(token: str) -> Dict[str, Any]:
    """
    Verifies a Keycloak access_token using JWKS (RS256).
    Returns decoded claims if valid.
    """
    try:
        key = _get_signing_key(token)
        issuer = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}"

        # We validate issuer. We do NOT validate 'aud' strictly here because
        # Keycloak access tokens often have aud=account by default.
        # For production you may validate audience / azp depending on your config.
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False},
        )
        return claims
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Keycloak JWKS fetch failed: {e}")
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid Keycloak token: {e}")


def get_keycloak_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Dict[str, Any]:
    """
    Dependency to protect endpoints with Keycloak.
    Returns claims including email/preferred_username.
    """
    token = credentials.credentials
    claims = verify_keycloak_token(token)

    email = (claims.get("email") or "").strip().lower()
    preferred = (claims.get("preferred_username") or "").strip().lower()

    if not email and not preferred:
        raise HTTPException(status_code=401, detail="Keycloak token missing email/username claim")

    return claims