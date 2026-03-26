import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.integrations.keycloak_device_flow import KeycloakDeviceFlow

from fastapi import Depends, Request
from backend.auth.keycloak_auth import get_keycloak_user


router = APIRouter(prefix="/keycloak-sharing", tags=["Keycloak Secure Sharing"])

KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "zkp-realm")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "zkp-device-client")

flow = KeycloakDeviceFlow(
    base_url=KEYCLOAK_URL,
    realm=KEYCLOAK_REALM,
    client_id=KEYCLOAK_CLIENT_ID,
    timeout=int(os.getenv("KEYCLOAK_DEVICE_TIMEOUT", "180")),
)


class DeviceStartResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    expires_in: int
    interval: int = 5


class DevicePollRequest(BaseModel):
    device_code: str
    interval: int = 5


@router.post("/device/start", response_model=DeviceStartResponse)
def device_start():
    """
    Step 1: start device authorization flow.
    """
    try:
        data = flow.start()
        return data
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/device/poll")
def device_poll(req: DevicePollRequest):
    """
    Step 2: poll until user authorizes on Keycloak.
    Returns access_token/refresh_token.
    """
    try:
        token = flow.poll_for_token(req.device_code, interval=req.interval)
        return token
    except TimeoutError as e:
        raise HTTPException(status_code=408, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    








class ProtectedSecretRequest(BaseModel):
    credential_id: int


@router.post("/secret")
def get_shared_secret(
    req: ProtectedSecretRequest,
    request: Request,
    claims: dict = Depends(get_keycloak_user),
):
    """
    Step 3 (FINAL):
    Requester calls this endpoint with:
      Authorization: Bearer <keycloak_access_token>

    For now (testing): returns a dummy secret + identity extracted from token.
    Later: you will link credential_id to your real DB logic.
    """
    email = (claims.get("email") or "").strip().lower()
    username = (claims.get("preferred_username") or "").strip()

    ip = request.client.host if request.client else "unknown"

    # Dummy secret for now (replace later with DB lookup + your sharing rules)
    return {
        "credential_id": req.credential_id,
        "secret": f"DUMMY_SECRET_FOR_CREDENTIAL_{req.credential_id}",
        "whoami": {
            "email": email,
            "preferred_username": username,
            "ip": ip,
        },
        "message": "OK — Keycloak token validated. Replace dummy secret with DB-backed share logic.",
    }