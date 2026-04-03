from __future__ import annotations

import hashlib
import time
from typing import Any, Dict

import jwt  # pip install PyJWT


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_delegation_token(secret: str, payload: Dict[str, Any], ttl_seconds: int) -> str:
    now = int(time.time())
    claims = {
        **payload,
        "iat": now,
        "exp": now + ttl_seconds,
        "typ": "delegation",
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def verify_delegation_token(secret: str, token: str) -> Dict[str, Any]:
    data = jwt.decode(token, secret, algorithms=["HS256"])
    if data.get("typ") != "delegation":
        raise ValueError("not a delegation token")
    return data