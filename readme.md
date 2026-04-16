# ZKP Credential Sharing — Passwordless Access Handoff (FastAPI + Streamlit + Chrome Extension + Keycloak)

A complete end‑to‑end prototype for **Zero‑Knowledge credential management and secure credential sharing** where the recipient can gain access to a third‑party application **without ever seeing the password**.

This repository demonstrates an applied security workflow combining:

- **Zero‑Knowledge Proof (ZKP) authentication** for the owner (no password transmitted).
- **Client‑side AES‑256‑GCM encryption** of secrets.
- **Ephemeral, revocable sharing tokens** for secure sharing.
- **Relay Login (Playwright)** performed server‑side to obtain a logged‑in session.
- **Chrome Extension cookie/storage injection** to open a connected profile without revealing credentials.
- **Keycloak Device Authorization Grant (passwordless)** to authenticate the recipient before completing a handoff.

---

## Why this project matters

Sharing access usually means sharing a password — which is unsafe, un-auditable, and hard to revoke.

This project implements a stronger model:

> The owner keeps the secret protected and only authorizes a temporary session handoff.  
> The recipient receives **session access**, not the password.

This is the same philosophy used by modern privileged access systems: **just‑in‑time access**, **least privilege**, **short-lived sessions**, **revocation**, **audit**.

---

## Architecture overview

### Components

- **FastAPI backend**
  - ZKP authentication (Schnorr-like flow)
  - encrypted credential storage & secure sharing rules
  - relay login via Playwright
  - session handoff endpoints for the extension
  - Keycloak integration (Device Flow + JWT validation via JWKS)

- **Streamlit dashboard**
  - owner UI: ZKP login, credential management, sharing, audit visibility
  - recipient UI: passwordless authorization flows and handoff generation

- **Chrome extension**
  - fetches handoff session once
  - injects cookies + localStorage + sessionStorage
  - opens the target app already logged in

- **Keycloak (optional module)**
  - recipient authentication via OAuth2 Device Authorization Grant
  - access tokens verified by backend via Keycloak JWKS (public keys)

---

## Core security properties

- **Passwords are never displayed to the recipient** in passwordless share mode.
- **Secrets remain encrypted** and are only decrypted server‑side when performing the relay login flow.
- **Short-lived, one-time handoff sessions** prevent replay.
- **Revocation support** (shares can be revoked immediately).
- **Audit trail** (access can be logged and traced).
- **Keycloak token validation** uses JWKS (no shared secret required).

---

## Main flows

### 1) Owner ZKP Login (Zero‑Knowledge)
The owner authenticates using a ZKP protocol:
- client derives secret locally
- server verifies proof
- backend issues an app JWT session

### 2) Secure Sharing (token‑based)
The owner creates a share entry (ephemeral, revocable, TTL), producing a `share_id` / share token depending on the flow.

### 3) Secure Relay Login (no password reveal)
Instead of sending the password:
- backend runs Playwright to log in to the target site
- backend stores cookies/storage server-side temporarily
- extension retrieves them using a one-time `session_id`
- extension injects session into recipient’s browser and opens connected profile

### 4) Keycloak Passwordless Share (Device Flow)
Recipient can be authenticated via Keycloak Device Authorization Grant:
- owner generates device flow + handoff session (`kc_session_id`)
- recipient logs in on Keycloak using `verification_uri_complete`
- backend polls Keycloak, verifies token (JWKS), then performs relay login
- recipient receives only a `handoff session_id` (never the password)

---

## Repository layout

- `backend/`
  - `main.py` — FastAPI app entrypoint & router wiring
  - `auth/` — ZKP + Keycloak token validation (JWKS)
  - `integrations/` — Keycloak device flow client
  - `routers/`
    - `auth.py` — ZKP login endpoints
    - `credentials.py` — CRUD for credentials
    - `sharing.py` — secure sharing + relay-login + handoff endpoints
    - `keycloak_sharing.py` — device flow & protected test endpoints
    - `extension_bridge.py` — optional bridge URL for “no copy/paste” UX
  - `relay/playwright_relay.py` — Playwright login & session extraction

- `frontend/`
  - `dashboard.py` — Streamlit dashboard UI

- `browser-extension/` (or your extension folder)
  - extension code to fetch `/sharing/handoff/{session_id}` and inject

---

## Quickstart (local)

### 1) Start Keycloak (optional, for passwordless recipient auth)
- Keycloak base URL: `http://localhost:8080`
- Realm: `zkp-realm`
- Make sure Device Flow is enabled for your client (e.g. `zkp-device-client`)

### 2) Run backend
```bash
# from repo root
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# recommended: avoid visible Playwright browser windows during relay login
export PLAYWRIGHT_HEADLESS=1

uvicorn backend.main:app --reload --port 8001

uvicorn backend.main:app --host 0.0.0.0 --port 8001 --ssl-keyfile=key.pem --ssl-certfile=cert.pem 

```

Check:
- `https://localhost:8001/health`
- `https://localhost:8001/docs`

### 3) Run Streamlit dashboard
```bash
export ZKP_API_URL=http://localhost:8001
streamlit run frontend/dashboard.py --server.port 8502


streamlit run frontend/dashboard.py \  --server.sslCertFile=cert.pem \
  --server.sslKeyFile=key.pem \
  --server.port 8502



  
```

Open:
- `http://localhost:8502`

### 4) Load the Chrome extension
- Open Chrome → `chrome://extensions`
- Enable **Developer mode**
- **Load unpacked** → select the extension folder
- Ensure permissions allow:
  - `http://localhost:8001/*`
  - target website domains (e.g. `https://recolyse.com/*`)

---

## Testing (step-by-step)

### A) Secure Relay Login + Extension injection (no password reveal)

**Owner**
1. Log in via **ZKP Login** in Streamlit.
2. Create a credential (service URL, username, password).
3. Create a share entry (recipient email + TTL, etc.).
4. Trigger Relay Login (backend runs Playwright).
5. Backend returns a one-time `handoff session_id`.

**Recipient**
6. Use the extension to fetch the handoff session and inject cookies/storage.
7. Browser opens the connected profile automatically.

Expected:
- password is never displayed to recipient
- handoff URL/session is short-lived + one-time

### B) Keycloak Passwordless Share (Device Authorization Grant)

**Owner**
1. Log in via ZKP.
2. Generate a passwordless handoff link (device flow).
3. Share the `verification_uri_complete` (and/or recipient link) with the recipient.

**Recipient**
4. Open `verification_uri_complete`, log in on Keycloak, approve.
5. Finalize handoff (backend polls token + relay login).
6. Use extension injection to open connected profile.

Expected:
- Keycloak token is verified by backend JWKS
- no password reveal
- one-time handoff + TTL

---

## Environment variables

Backend:
- `KEYCLOAK_URL` (default `http://localhost:8080`)
- `KEYCLOAK_REALM` (default `zkp-realm`)
- `KEYCLOAK_CLIENT_ID` (default `zkp-device-client`)
- `KEYCLOAK_DEVICE_TIMEOUT` (default `180`)
- `PLAYWRIGHT_HEADLESS` (`1` recommended)

Frontend:
- `ZKP_API_URL` (default `http://localhost:8001`)

---

## Notes & limitations

- This is a prototype; for production you would:
  - store temporary sessions in Redis/DB instead of in-memory dicts
  - enforce audience (`aud`) checks for Keycloak tokens
  - harden rate limits and brute-force protections
  - add stronger audit logging and export
  - consider per-domain relay profiles and anti-bot handling

---

## What’s next (roadmap ideas)

- ✅ Automatic “no copy/paste” flow via backend bridge URL + extension interception
- Redis-backed session store for horizontal scaling
- Recipient-specific policies (allowed domains, maximum sessions, time windows)
- Stronger Keycloak policy support (MFA, device trust, step-up auth)

---

## License
Add your license here (MIT/Apache-2.0/etc.).