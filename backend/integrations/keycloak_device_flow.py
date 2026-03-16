import time
from typing import Dict, Any, Optional

import requests


class KeycloakDeviceFlow:
    """
    Implements OAuth2 Device Authorization Grant against Keycloak.

    Key endpoints:
      - device authorization: /realms/{realm}/protocol/openid-connect/auth/device
      - token:              /realms/{realm}/protocol/openid-connect/token
    """

    def __init__(self, base_url: str, realm: str, client_id: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.timeout = timeout

    @property
    def device_endpoint(self) -> str:
        return f"{self.base_url}/realms/{self.realm}/protocol/openid-connect/auth/device"

    @property
    def token_endpoint(self) -> str:
        return f"{self.base_url}/realms/{self.realm}/protocol/openid-connect/token"

    def start(self, scope: str = "openid profile email") -> Dict[str, Any]:
        """
        Starts device flow; returns:
          device_code, user_code, verification_uri, verification_uri_complete, expires_in, interval
        """
        r = requests.post(
            self.device_endpoint,
            data={"client_id": self.client_id, "scope": scope},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def poll_for_token(self, device_code: str, interval: int = 5) -> Dict[str, Any]:
        """
        Polls until token is granted or timeout.
        """
        start = time.time()
        while True:
            if time.time() - start > self.timeout:
                raise TimeoutError("Device flow timed out")

            r = requests.post(
                self.token_endpoint,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.client_id,
                    "device_code": device_code,
                },
                timeout=15,
            )

            # Keycloak returns JSON error like:
            # {"error":"authorization_pending"} or {"error":"slow_down"} ...
            if r.status_code == 200:
                return r.json()

            data: Optional[Dict[str, Any]] = None
            try:
                data = r.json()
            except Exception:
                pass

            err = (data or {}).get("error")
            if err == "authorization_pending":
                time.sleep(interval)
                continue
            if err == "slow_down":
                interval = interval + 2
                time.sleep(interval)
                continue

            # terminal errors: access_denied, expired_token, invalid_request...
            raise ValueError(f"Device flow failed: {data or r.text}")