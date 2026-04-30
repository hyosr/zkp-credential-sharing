````md
# ZKP Credential Sharing

**Passwordless Access Handoff with FastAPI, Streamlit, Chrome Extension, and Keycloak**

A complete end-to-end prototype for secure credential sharing and access handoff, where the recipient can gain access to a third-party application **without ever seeing the password**.

---

## Overview

This repository demonstrates an applied security workflow that combines:

- **Zero-Knowledge Proof (ZKP) authentication** for the owner, with no password transmitted
- **Client-side AES-256-GCM encryption** of stored secrets
- **Ephemeral, revocable sharing tokens** for secure access handoff
- **Relay login** via Playwright to obtain a logged-in session server-side
- **Chrome Extension cookie/storage injection** to open a connected profile without exposing credentials
- **Keycloak Device Authorization Grant** for passwordless recipient authentication

---

## Why this project matters

Sharing access usually means sharing a password, which is unsafe, difficult to audit, and hard to revoke.

This project implements a stronger model:

- The owner keeps the secret protected
- Access is granted only as a **temporary session handoff**
- The recipient receives **session access**, not the password

This follows the principles of modern privileged access systems:

- Just-in-time access
- Least privilege
- Short-lived sessions
- Revocation
- Auditability

---

## Key Features

- **ZKP-based owner authentication**
- **Encrypted credential storage**
- **Secure sharing with TTL and revocation**
- **Relay login automation with Playwright**
- **One-time handoff sessions**
- **Chrome extension session injection**
- **Keycloak passwordless recipient flow**
- **JWT validation using JWKS**
- **Audit-friendly architecture**

---

## Architecture

### Backend: FastAPI

Responsible for:

- ZKP authentication
- Secure credential storage and sharing rules
- Relay login via Playwright
- Session handoff endpoints for the extension
- Keycloak integration and JWT validation via JWKS

### Frontend: Streamlit Dashboard

Provides:

- Owner login via ZKP
- Credential management
- Secure sharing workflow
- Visibility into handoff and access events
- Recipient passwordless authorization flow

### Chrome Extension

Used to:

- Fetch a one-time handoff session
- Inject cookies, `localStorage`, and `sessionStorage`
- Open the target app already logged in

### Keycloak Integration

Used for:

- Passwordless recipient authentication
- OAuth2 Device Authorization Grant
- Token verification through backend-side JWKS validation

---

## Security Properties

- Passwords are **never displayed** to the recipient in passwordless share mode
- Secrets remain encrypted and are only decrypted server-side during relay login
- Handoff sessions are **short-lived** and **one-time use**
- Shares can be revoked immediately
- Access can be logged and audited
- Keycloak tokens are validated using **JWKS**, with no shared secret required

---

## Main Flows

### 1. Owner ZKP Login

The owner authenticates using a Zero-Knowledge Proof flow:

1. The client derives the secret locally
2. The server verifies the proof
3. The backend issues an application session token

### 2. Secure Sharing

The owner creates a share entry with:

- recipient email
- TTL
- revocation support
- ephemeral share identifier

### 3. Secure Relay Login

Instead of sending the password:

1. The backend uses Playwright to log in to the target site
2. The backend stores cookies and session data temporarily
3. The extension retrieves them using a one-time `session_id`
4. The extension injects the session into the recipient browser

### 4. Keycloak Passwordless Share

For the recipient:

1. The owner generates a device-flow handoff session
2. The recipient authenticates via Keycloak
3. The backend polls and validates the token
4. The relay login is completed
5. The recipient receives only a session handoff, never the password

---

## Repository Structure

```text
backend/
  main.py                  # FastAPI app entrypoint and router wiring
  auth/                    # ZKP and Keycloak token validation
  integrations/            # Keycloak device flow client
  routers/
    auth.py                # ZKP login endpoints
    credentials.py         # CRUD for credentials
    sharing.py              # Secure sharing + relay login + handoff endpoints
    keycloak_sharing.py     # Device flow and protected test endpoints
    extension_bridge.py     # Optional bridge URL for no copy/paste UX
  relay/
    playwright_relay.py     # Playwright login and session extraction

frontend/
  dashboard.py              # Streamlit dashboard UI

browser-extension/          # Chrome extension code
````

---

## Quickstart

### 1. Start Keycloak (optional)

Keycloak base URL: `http://localhost:8080`
Realm: `zkp-realm`

Make sure **Device Flow** is enabled for your client, for example `zkp-device-client`.

### 2. Run the backend

```bash
# from repo root
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# recommended: avoid visible Playwright browser windows during relay login
export PLAYWRIGHT_HEADLESS=1

uvicorn backend.main:app --reload --port 8001
```

Optional HTTPS:

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8001 --ssl-keyfile=key.pem --ssl-certfile=cert.pem
```

Check:

* `https://localhost:8001/health`
* `https://localhost:8001/docs`

### 3. Run the Streamlit dashboard

```bash
export ZKP_API_URL=http://localhost:8001

streamlit run frontend/dashboard.py
```

Optional port and HTTPS:

```bash
streamlit run frontend/dashboard.py --server.port 8502

streamlit run frontend/dashboard.py \
  --server.sslCertFile=cert.pem \
  --server.sslKeyFile=key.pem \
  --server.port=8502
```

Then open:

* `http://localhost:8502`

### 4. Load the Chrome extension

1. Open Chrome
2. Go to `chrome://extensions`
3. Enable **Developer mode**
4. Click **Load unpacked**
5. Select the extension folder

Ensure permissions allow access to:

* `http://localhost:8001/*`
* target website domains, for example `https://recolyse.com/*`

---

## Testing Scenarios

### A. Secure Relay Login + Extension Injection

**Owner side**

1. Log in via ZKP in Streamlit
2. Create a credential with service URL, username, and password
3. Create a share entry with recipient email and TTL
4. Trigger relay login
5. Backend returns a one-time handoff `session_id`

**Recipient side**

6. Use the extension to fetch the handoff session
7. Inject cookies and storage
8. Browser opens the connected profile automatically

**Expected result**

* Password is never displayed to the recipient
* Handoff session is short-lived and one-time


## Environment Variables

### Backend

| Variable                  |                 Default | Description                     |
| ------------------------- | ----------------------: | ------------------------------- |

| `PLAYWRIGHT_HEADLESS`     |                     `1` | Run Playwright in headless mode |

### Frontend

| Variable      |                 Default | Description          |
| ------------- | ----------------------: | -------------------- |
| `ZKP_API_URL` | `http://localhost:8001` | Backend API base URL |


---



