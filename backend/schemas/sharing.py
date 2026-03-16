from pydantic import BaseModel, Field
from typing import Optional, Dict, Any


class ShareCreateRequest(BaseModel):
    credential_id: int
    recipient_email: str
    permission: str = "read_once"
    ttl_hours: int = 24
    max_uses: int = 1

    # Data encrypted by the owner client-side with the one-time share key
    # This plaintext should be a JSON string that includes at least: {"password": "..."}
    encrypted_payload: str

    # optional metadata for relay automation (selectors)
    relay_profile: Optional[Dict[str, Any]] = None


class ShareAccessRequest(BaseModel):
    token: str
    requester_email: str


class RelayLoginRequest(BaseModel):
    token: str
    requester_email: str

    # If you want to override service_url at runtime (optional)
    service_url_override: Optional[str] = None
    