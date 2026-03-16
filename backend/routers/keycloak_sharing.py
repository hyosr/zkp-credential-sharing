import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.integrations.keycloak_device_flow import KeycloakDeviceFlow

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