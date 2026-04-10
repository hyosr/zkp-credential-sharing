"""
ZKP Secure Credential Sharing - Streamlit Dashboard
=====================================================
Complete user interface for Part 2.
Implements the CLIENT side of the ZKP protocol:
  - Derivation of secret x on the client side
  - Generation of commitment g^r mod p
  - Computation of response s = r - c*x mod q
  - Local AES-256-GCM encryption/decryption
"""

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Optional

import urllib

import requests
import streamlit as st
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ─── Configuration ────────────────────────────────────────────────────────────

API_URL = os.getenv("ZKP_API_URL", "http://localhost:8001")

# ZKP parameters (identical to the server)
P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF",
    16,
)
G = 2
Q = (P - 1) // 2


# ─── Client‑Side ZKP Functions ────────────────────────────────────────────────

def client_derive_secret(password: str, salt_b64: str) -> int:
    """Derive secret x from password (client side)."""
    salt = base64.b64decode(salt_b64)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations=310_000, dklen=32)
    return int.from_bytes(dk, "big") % (Q - 1) + 1


def client_generate_public_key(password: str) -> tuple:
    """Generate (Y=g^x mod p, salt_b64)."""
    salt = os.urandom(32)
    x = client_derive_secret(password, base64.b64encode(salt).decode())
    Y = pow(G, x, P)
    return hex(Y), base64.b64encode(salt).decode()


def client_create_commitment() -> tuple:
    """Generate (Y_r=g^r mod p, r)."""
    r = secrets.randbelow(Q - 1) + 1
    Y_r = pow(G, r, P)
    return hex(Y_r), r


def client_compute_response(password: str, salt_b64: str, r: int, challenge_hex: str) -> str:
    """Compute s = r - c*x mod q."""
    x = client_derive_secret(password, salt_b64)
    c = int(challenge_hex, 16)
    s = (r - c * x) % Q
    return hex(s)


def client_encrypt(plaintext: str, password: str, salt_b64: str) -> str:
    """Encrypt locally with AES-256-GCM (key derived from master password)."""
    salt = base64.b64decode(salt_b64)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations=310_000, dklen=32)
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return json.dumps({
        "salt": salt_b64,
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ct).decode(),
    })


def client_decrypt(encrypted_json: str, password: str) -> str:
    """Decrypt locally with AES-256-GCM."""
    d = json.loads(encrypted_json)
    salt = base64.b64decode(d["salt"])
    nonce = base64.b64decode(d["nonce"])
    ct = base64.b64decode(d["ciphertext"])
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations=310_000, dklen=32)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()


def _b64decode_urlsafe_padded(s: str) -> bytes:
    s = (s or "").strip()
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("utf-8"))


def encrypt_for_share(plaintext: str, share_token: str) -> str:
    """Encrypt a secret with an ephemeral share token."""
    raw = _b64decode_urlsafe_padded(share_token)
    key = raw[:32]
    if len(key) != 32:
        raise ValueError("Share token invalid (incorrect key).")

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return json.dumps({
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ct).decode(),
    })


def decrypt_from_share(encrypted_json: str, share_token: str) -> str:
    """Decrypt with the share token."""
    d = json.loads(encrypted_json)
    raw = _b64decode_urlsafe_padded(share_token)
    key = raw[:32]
    if len(key) != 32:
        raise ValueError("Share token invalid (incorrect key).")

    nonce = base64.b64decode(d["nonce"])
    ct = base64.b64decode(d["ciphertext"])
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()


# ─── API Helpers ──────────────────────────────────────────────────────────────

def api_post(endpoint: str, data: dict, token: str = None) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(f"{API_URL}{endpoint}", json=data, headers=headers, timeout=120)

        # Always return status + body if not JSON
        content_type = (r.headers.get("content-type") or "").lower()
        if "application/json" not in content_type:
            return {
                "error": "Non-JSON response from API",
                "status_code": r.status_code,
                "content_type": content_type,
                "text": r.text[:2000],  # for debug
                "endpoint": endpoint,
            }

        return r.json()
    except Exception as e:
        return {"error": str(e), "endpoint": endpoint}


def api_get(endpoint: str, token: str = None) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(f"{API_URL}{endpoint}", headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": str(e)}
    



def api_delete(path: str, token: str):
    url = f"{API_URL}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.delete(url, headers=headers, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"status": r.status_code, "text": r.text}











def api_post_strict(endpoint: str, data: dict, token: str = None, timeout: int = 60) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.post(f"{API_URL}{endpoint}", json=data, headers=headers, timeout=timeout)
    try:
        j = r.json()
    except Exception:
        j = {"status_code": r.status_code, "text": r.text[:2000]}
    if r.status_code >= 400:
        # normalize error
        detail = j.get("detail") if isinstance(j, dict) else None
        return {"error": True, "status_code": r.status_code, "detail": detail or j}
    return j


def api_get_strict(endpoint: str, token: str = None, timeout: int = 30) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(f"{API_URL}{endpoint}", headers=headers, timeout=timeout)
    try:
        j = r.json()
    except Exception:
        j = {"status_code": r.status_code, "text": r.text[:2000]}
    if r.status_code >= 400:
        detail = j.get("detail") if isinstance(j, dict) else None
        return {"error": True, "status_code": r.status_code, "detail": detail or j}
    return j








# ─── UI THEME (visual only) ───────────────────────────────────────────────────



def make_extension_connect_url(handoff_url: str) -> str:
    # This is a simple “bridge URL” your extension will listen to
    # It can be any domain you own; for local demo we use your API
    return f"{API_URL}/extension/connect?handoff={urllib.parse.quote(handoff_url, safe='')}"






# def _inject_theme():
#     st.markdown(
#         """
#         <style>
#           /* -------- Global cyber theme -------- */
#           .stApp {
#             background: radial-gradient(1200px 700px at 20% 0%, rgba(0, 212, 255, 0.10), transparent 60%),
#                         radial-gradient(900px 600px at 90% 30%, rgba(141, 78, 255, 0.10), transparent 55%),
#                         linear-gradient(180deg, #0b0f17 0%, #0b0f17 100%);
#             color: #e6edf3;
#           }

#           /* Reduce top padding a bit */
#           .block-container { padding-top: 1.2rem; }

#           /* Sidebar */
#           [data-testid="stSidebar"] {
#             background: linear-gradient(180deg, #0c1220 0%, #0a0f1a 100%);
#             border-right: 1px solid rgba(255,255,255,0.06);
#           }

#           /* Headings */
#           h1, h2, h3 {
#             letter-spacing: 0.2px;
#           }

#           /* Cards */
#           .zkp-card {
#             background: rgba(255,255,255,0.04);
#             border: 1px solid rgba(255,255,255,0.08);
#             border-radius: 16px;
#             padding: 16px 18px;
#             box-shadow: 0 10px 30px rgba(0,0,0,0.30);
#             backdrop-filter: blur(6px);
#           }

#           .zkp-hero {
#             background: linear-gradient(135deg, rgba(0,212,255,0.10), rgba(141,78,255,0.10));
#             border: 1px solid rgba(0,212,255,0.18);
#             border-radius: 18px;
#             padding: 18px 18px;
#             margin-bottom: 14px;
#           }

#           .zkp-badge {
#             display: inline-block;
#             background: linear-gradient(135deg, rgba(0,212,255,0.22), rgba(141,78,255,0.16));
#             color:#aeeaff;
#             padding: 5px 12px;
#             border-radius: 999px;
#             font-size: 12px;
#             font-weight: 700;
#             border: 1px solid rgba(0,212,255,0.25);
#           }

#           .zkp-muted { color: rgba(230,237,243,0.70); }

#           /* Alert boxes */
#           .zkp-success {
#             background: rgba(46, 204, 113, 0.08);
#             border: 1px solid rgba(46, 204, 113, 0.20);
#             border-left: 4px solid rgba(46, 204, 113, 0.70);
#             padding: 12px 14px;
#             border-radius: 12px;
#             margin: 10px 0;
#           }
#           .zkp-warning {
#             background: rgba(241, 196, 15, 0.07);
#             border: 1px solid rgba(241, 196, 15, 0.20);
#             border-left: 4px solid rgba(241, 196, 15, 0.70);
#             padding: 12px 14px;
#             border-radius: 12px;
#             margin: 10px 0;
#           }
#           .zkp-danger {
#             background: rgba(231, 76, 60, 0.08);
#             border: 1px solid rgba(231, 76, 60, 0.18);
#             border-left: 4px solid rgba(231, 76, 60, 0.70);
#             padding: 12px 14px;
#             border-radius: 12px;
#             margin: 10px 0;
#           }

#           /* Buttons: make them look more "product" */
#           .stButton > button {
#             border-radius: 12px;
#             border: 1px solid rgba(255,255,255,0.12);
#             background: linear-gradient(135deg, rgba(0,212,255,0.14), rgba(141,78,255,0.12));
#             color: #e6edf3;
#             font-weight: 700;
#             padding: 0.55rem 0.9rem;
#           }
#           .stButton > button:hover {
#             border-color: rgba(0,212,255,0.28);
#             background: linear-gradient(135deg, rgba(0,212,255,0.18), rgba(141,78,255,0.16));
#           }

#           /* Inputs */
#           .stTextInput input, .stTextArea textarea, .stSelectbox div, .stNumberInput input {
#             border-radius: 12px !important;
#           }

#           /* Expanders */
#           .streamlit-expanderHeader {
#             background: rgba(255,255,255,0.04) !important;
#             border: 1px solid rgba(255,255,255,0.08) !important;
#             border-radius: 12px !important;
#             color: #e6edf3 !important;
#           }

#           /* Small code blocks */
#           code, pre {
#             border-radius: 12px !important;
#           }

#           /* Hide Streamlit menu/footer (optional but makes it feel more "app") */
#           #MainMenu {visibility: hidden;}
#           footer {visibility: hidden;}
#         </style>
#         """,
#         unsafe_allow_html=True,
#     )







def _inject_theme():
    st.markdown(
        """
        <style>
          :root {
            --bg0: #060b14;
            --bg1: #0a1220;
            --panel: rgba(255,255,255,0.045);
            --panel2: rgba(255,255,255,0.06);
            --border: rgba(255,255,255,0.10);

            --text: #e6edf3;
            --muted: rgba(230,237,243,0.72);

            --blue: #2f81f7;
            --cyan: #00d4ff;

            --ok: rgba(46, 204, 113, 0.12);
            --warn: rgba(241, 196, 15, 0.10);
            --err: rgba(231, 76, 60, 0.10);
          }

          /* App background: deep blue, no purple */
          .stApp {
            background:
              radial-gradient(900px 500px at 15% 0%, rgba(0, 212, 255, 0.10), transparent 55%),
              radial-gradient(900px 600px at 85% 35%, rgba(47, 129, 247, 0.10), transparent 60%),
              linear-gradient(180deg, var(--bg0) 0%, var(--bg0) 100%);
            color: var(--text);
          }

          .block-container { padding-top: 1.0rem; }

          /* Sidebar */
          [data-testid="stSidebar"] {
            background: linear-gradient(180deg, var(--bg1) 0%, var(--bg0) 100%);
            border-right: 1px solid rgba(255,255,255,0.06);
          }

          h1,h2,h3 { letter-spacing: 0.2px; }

          .zkp-card {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 14px 16px;
            box-shadow: 0 12px 30px rgba(0,0,0,0.30);
            backdrop-filter: blur(6px);
          }

          .zkp-hero {
            background: linear-gradient(135deg, rgba(0,212,255,0.10), rgba(47,129,247,0.10));
            border: 1px solid rgba(0,212,255,0.18);
            border-radius: 16px;
            padding: 16px 16px;
            margin-bottom: 12px;
          }

          .zkp-badge {
            display: inline-block;
            background: linear-gradient(135deg, rgba(0,212,255,0.20), rgba(47,129,247,0.14));
            color: #bfefff;
            padding: 5px 12px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 800;
            border: 1px solid rgba(0,212,255,0.22);
          }

          .zkp-muted { color: var(--muted); }

          .zkp-success {
            background: var(--ok);
            border: 1px solid rgba(46, 204, 113, 0.22);
            border-left: 4px solid rgba(46, 204, 113, 0.70);
            padding: 10px 12px;
            border-radius: 12px;
            margin: 10px 0;
          }
          .zkp-warning {
            background: var(--warn);
            border: 1px solid rgba(241, 196, 15, 0.22);
            border-left: 4px solid rgba(241, 196, 15, 0.75);
            padding: 10px 12px;
            border-radius: 12px;
            margin: 10px 0;
          }
          .zkp-danger {
            background: var(--err);
            border: 1px solid rgba(231, 76, 60, 0.22);
            border-left: 4px solid rgba(231, 76, 60, 0.75);
            padding: 10px 12px;
            border-radius: 12px;
            margin: 10px 0;
          }

          /* Buttons */
          .stButton > button {
            border-radius: 12px;
            border: 1px solid rgba(255,255,255,0.12);
            background: linear-gradient(135deg, rgba(0,212,255,0.16), rgba(47,129,247,0.14));
            color: var(--text);
            font-weight: 800;
            padding: 0.55rem 0.9rem;
          }
          .stButton > button:hover {
            border-color: rgba(0,212,255,0.30);
            background: linear-gradient(135deg, rgba(0,212,255,0.20), rgba(47,129,247,0.18));
          }

          /* Inputs */
          .stTextInput input, .stTextArea textarea, .stSelectbox div, .stNumberInput input {
            border-radius: 12px !important;
          }

          /* Expanders */
          .streamlit-expanderHeader {
            background: var(--panel2) !important;
            border: 1px solid var(--border) !important;
            border-radius: 12px !important;
            color: var(--text) !important;
          }

          code, pre { border-radius: 12px !important; }

          #MainMenu {visibility: hidden;}
          footer {visibility: hidden;}
        </style>
        """,
        unsafe_allow_html=True,
    )















# def _render_top_header():
#     st.markdown(
#         """
#         <div class="zkp-hero">
#           <div style="display:flex; align-items:center; justify-content:space-between; gap:16px;">
#             <div>
#               <div class="zkp-badge">ZERO‑KNOWLEDGE • SCHNORR • AES‑GCM</div>
#               <h1 style="margin:10px 0 2px 0;">ZKP Secure Credential Sharing</h1>
#               <div class="zkp-muted">
#                 Passwordless proof‑of‑knowledge login and secure secret sharing — designed for Zero‑Trust environments.
#               </div>
#             </div>
#             <div style="text-align:right;">
#               <div class="zkp-muted" style="font-size:12px;">API Endpoint</div>
#               <div style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
#                           font-size:13px; padding:10px 12px; border-radius:12px;
#                           background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);">
#                 %s
#               </div>
#             </div>
#           </div>
#         </div>
#         """ % API_URL,
#         unsafe_allow_html=True,
#     )





def _render_top_header():
    st.markdown(
        f"""
        <div class="zkp-hero">
          <div style="display:flex; align-items:flex-start; justify-content:space-between; gap:16px;">
            <div>
              <div class="zkp-badge">ZKP • Schnorr • AES‑GCM</div>
              <h1 style="margin:10px 0 2px 0;">ZKP Credential Sharing</h1>
              <div class="zkp-muted">Secure credential storage + relay login (no password reveal).</div>
            </div>
            <div style="text-align:right;">
              <div class="zkp-muted" style="font-size:12px;">API</div>
              <div style="font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
                          font-size:13px; padding:10px 12px; border-radius:12px;
                          background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);">
                {API_URL}
              </div>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

















def _init_session_state():
    if "jwt_token" not in st.session_state:
        st.session_state.jwt_token = None
        st.session_state.current_user = None
        st.session_state.master_password = None
        st.session_state.zkp_salt = None
        st.session_state.master_salt = None


def _logout():
    for k in ["jwt_token", "current_user", "master_password", "zkp_salt", "master_salt"]:
        st.session_state[k] = None
    st.rerun()


def _render_sidebar(menu_logged_out, menu_logged_in):
    with st.sidebar:
        st.markdown("## Control Panel")
        st.markdown('<span class="zkp-badge">SECURE MODE</span>', unsafe_allow_html=True)
        st.markdown("---")

        if st.session_state.jwt_token:
            st.markdown(
                f"""
                <div class="zkp-card">
                  <div style="font-size:12px;" class="zkp-muted">Signed in as</div>
                  <div style="font-weight:800; font-size:16px;">{st.session_state.current_user}</div>
                  <div style="margin-top:10px; display:flex; gap:10px;">
                    <div style="flex:1; background: rgba(46,204,113,0.10); border:1px solid rgba(46,204,113,0.18);
                                padding:8px 10px; border-radius:12px;">
                      <div style="font-size:11px;" class="zkp-muted">Session</div>
                      <div style="font-weight:800;">ACTIVE</div>
                    </div>
                    <div style="flex:1; background: rgba(0,212,255,0.08); border:1px solid rgba(0,212,255,0.16);
                                padding:8px 10px; border-radius:12px;">
                      <div style="font-size:11px;" class="zkp-muted">Crypto</div>
                      <div style="font-weight:800;">LOCAL</div>
                    </div>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.write("")
            if st.button("Logout", use_container_width=True):
                _logout()

            st.markdown("---")
            menu = st.radio("Navigation", menu_logged_in)
        else:
            st.markdown(
                """
                <div class="zkp-card">
                  <div style="font-weight:800; font-size:15px;">Welcome</div>
                  <div class="zkp-muted" style="margin-top:6px; font-size:13px;">
                    Use ZKP to authenticate without sending the password to the server.
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown("---")
            menu = st.radio("Navigation", menu_logged_out)

        st.markdown("---")
        with st.expander("Operational Notes"):
            st.markdown(
                """
- Your password is used **only locally** to derive secrets (PBKDF2).
- ZKP proofs are generated **client-side**.
- Stored secrets remain encrypted (AES‑256‑GCM).
                """
            )

    return menu


# ─── Streamlit UI ─────────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="ZKP Secure Credential Sharing",
        page_icon="🔐",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    _inject_theme()
    _init_session_state()
    _render_top_header()

    # Sidebar menu options (same items, just organized/renamed visually)
    menu_logged_in = [
        "🔑 My Credentials",
        "➕ New Credential",
        "🤝 Share",
        "📩 Access a Share",
        "🚪 Relay Login (no password reveal)",
        "👥 Assisted Relay Login",
        "🛎️ Owner — Assisted Requests",
        # "🔐 Keycloak Passwordless Share (Device Flow)",
        "📋 Audit Trail",
        "ℹ️ About ZKP",
    ]
    menu_logged_out = ["🔐 ZKP Login", "📝 Register", "ℹ️ About ZKP"]

    menu = _render_sidebar(menu_logged_out, menu_logged_in)

    # Main content wrapper (visual only)
    st.markdown('<div class="zkp-card">', unsafe_allow_html=True)

    # ─── Pages ────────────────────────────────────────────────────────────────
    if menu == "🔐 ZKP Login":
        page_login()
    elif menu == "📝 Register":
        page_register()
    elif menu == "🔑 My Credentials":
        page_credentials()
    elif menu == "➕ New Credential":
        page_new_credential()
    elif menu == "🤝 Share":
        page_share()
    elif menu == "📩 Access a Share":
        page_access_share()
    elif menu == "📋 Audit Trail":
        page_audit()
    elif menu == "ℹ️ About ZKP":
        page_about_zkp()
    elif menu == "🚪 Relay Login (no password reveal)":
        page_relay_login()
    elif menu == "🔐 Keycloak Passwordless Share (Device Flow)":
        page_keycloak_device_flow()
    
    elif menu == "🛎️ Owner — Assisted Requests":
        page_owner_assisted_requests()

    elif menu == "👥 Assisted Relay Login":
        page_assisted_relay_login()
    st.markdown("</div>", unsafe_allow_html=True)


# ─── Pages (FUNCTIONAL LOGIC UNCHANGED — only minor UI framing) ───────────────

# def page_login():
#     st.title("🔐 Zero-Knowledge Proof Login")
#     st.markdown(
#         """
#         <div class="zkp-warning">
#           Login uses a Zero‑Knowledge Proof (Schnorr). Your password never leaves the client.
#         </div>
#         """,
#         unsafe_allow_html=True,
#     )

#     col2 = st.columns([1, 1], gap="large")
    

#     with col2:
#         st.subheader("Login")
#         with st.form("login_form"):
#             email = st.text_input("Email")
#             password = st.text_input("Master password (stays local)", type="password")
#             submitted = st.form_submit_button("ZKP Login", use_container_width=True)

#         if submitted and email and password:
#             with st.spinner("Retrieving salts..."):
#                 salts = api_get(f"/auth/salts/{email}")
#             if "error" in salts or "zkp_salt" not in salts:
#                 st.error("User not found")
#                 return

#             zkp_salt = salts["zkp_salt"]
#             master_salt = salts["master_salt"]

#             with st.spinner("Generating ZKP commitment..."):
#                 commitment_hex, r = client_create_commitment()
#                 resp_challenge = api_post("/auth/challenge", {
#                     "email": email,
#                     "commitment": commitment_hex,
#                 })

#             if "error" in resp_challenge or "challenge_id" not in resp_challenge:
#                 st.error(f"Challenge error: {resp_challenge}")
#                 return

#             challenge_id = resp_challenge["challenge_id"]
#             challenge_hex = resp_challenge["challenge_value"]

#             with st.spinner("Computing ZKP proof..."):
#                 response_hex = client_compute_response(password, zkp_salt, r, challenge_hex)
#                 resp_verify = api_post("/auth/verify", {
#                     "email": email,
#                     "challenge_id": challenge_id,
#                     "response": response_hex,
#                 })

#             if "access_token" in resp_verify:
#                 st.session_state.jwt_token = resp_verify["access_token"]
#                 st.session_state.current_user = resp_verify["username"]
#                 st.session_state.master_password = password
#                 st.session_state.zkp_salt = zkp_salt
#                 st.session_state.master_salt = master_salt
#                 st.markdown(
#                     f"""
#                     <div class="zkp-success">
#                       ✅ <strong>ZKP login successful</strong> — Welcome <strong>{resp_verify['username']}</strong><br/>
#                       <span class="zkp-muted">Session token stored in memory (Streamlit session).</span>
#                     </div>
#                     """,
#                     unsafe_allow_html=True,
#                 )
#                 st.balloons()
#                 st.rerun()
#             else:
#                 st.error(f"Authentication failed: {resp_verify.get('detail', resp_verify)}")




def page_login():
    st.title("🔐 Zero-Knowledge Proof Login")
    st.markdown(
        """
        <div class="zkp-warning">
          Login uses a Zero‑Knowledge Proof (Schnorr). Your password never leaves the client.
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Formulaire de connexion (sans colonnes)
    st.subheader("Login")
    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Master password (stays local)", type="password")
        submitted = st.form_submit_button("ZKP Login", use_container_width=True)

    if submitted and email and password:
        with st.spinner("Retrieving salts..."):
            salts = api_get(f"/auth/salts/{email}")
        if "error" in salts or "zkp_salt" not in salts:
            st.error("User not found")
            return

        zkp_salt = salts["zkp_salt"]
        master_salt = salts["master_salt"]

        with st.spinner("Generating ZKP commitment..."):
            commitment_hex, r = client_create_commitment()
            resp_challenge = api_post("/auth/challenge", {
                "email": email,
                "commitment": commitment_hex,
            })

        if "error" in resp_challenge or "challenge_id" not in resp_challenge:
            st.error(f"Challenge error: {resp_challenge}")
            return

        challenge_id = resp_challenge["challenge_id"]
        challenge_hex = resp_challenge["challenge_value"]

        with st.spinner("Computing ZKP proof..."):
            response_hex = client_compute_response(password, zkp_salt, r, challenge_hex)
            resp_verify = api_post("/auth/verify", {
                "email": email,
                "challenge_id": challenge_id,
                "response": response_hex,
            })

        if "access_token" in resp_verify:
            st.session_state.jwt_token = resp_verify["access_token"]
            st.session_state.current_user = resp_verify["username"]
            st.session_state.master_password = password
            st.session_state.zkp_salt = zkp_salt
            st.session_state.master_salt = master_salt
            st.markdown(
                f"""
                <div class="zkp-success">
                  ✅ <strong>ZKP login successful</strong> — Welcome <strong>{resp_verify['username']}</strong><br/>
                  <span class="zkp-muted">Session token stored in memory (Streamlit session).</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.balloons()
            st.rerun()
        else:
            st.error(f"Authentication failed: {resp_verify.get('detail', resp_verify)}")





        



def page_assisted_relay_login():
    st.title("👥 Owner‑Assisted Relay Login")
    st.markdown("""
    Utilisez ce mode si le site cible a un CAPTCHA, une 2FA, ou une procédure de connexion complexe.
    Le propriétaire sera notifié et devra se connecter manuellement. Sa session vous sera ensuite transmise.
    """)

    token_input = st.text_input("🔑 Share token")
    if st.button("📨 Request access", use_container_width=True):
        if token_input:
            resp = api_post("/sharing/assisted/request", {"share_token": token_input}, token=st.session_state.jwt_token)
            if "request_id" in resp:
                st.session_state.assist_request_id = resp["request_id"]
                st.success(f"Demande créée (ID: {resp['request_id']}). En attente de l'approbation du propriétaire...")
            else:
                st.error(f"Erreur: {resp}")

    if "assist_request_id" in st.session_state:
        rid = st.session_state.assist_request_id
        status_resp = api_get(f"/sharing/assisted/{rid}/status", token=st.session_state.jwt_token)

        if isinstance(status_resp, dict):
            status = status_resp.get("status")
            handoff_session_id = status_resp.get("handoff_session_id")
            st.info(f"**Statut:** {status}")
            if status == "completed" and handoff_session_id:
                handoff_url = f"{API_URL}/sharing/handoff/{handoff_session_id}"
                st.success("✅ Le propriétaire a terminé la connexion !")
                st.markdown("### 🔗 Handoff URL (copiez‑la pour l’extension)")
                st.code(handoff_url, language="text")
                st.info("Collez cette URL dans l’extension (mode Handoff) et cliquez sur Connect.")
            elif status == "expired":
                st.warning("⏰ La demande a expiré. Veuillez en créer une nouvelle.")
                if st.button("🗑️ Supprimer cette demande"):
                    del st.session_state.assist_request_id
                    st.rerun()
            else:
                if st.button("🔄 Rafraîchir le statut", use_container_width=True):
                    st.rerun()
        else:
            st.error(f"Erreur lors de la récupération du statut: {status_resp}")
    else:
        st.info("Aucune demande en cours. Saisissez un share token et cliquez sur « Request access ».")














# def page_owner_assisted_requests():
#     st.title("👑 Owner — Assisted Requests")
#     st.markdown("Pending requests from recipients. Click 'Approve and assist' to help them log in.")
#     token = st.session_state.jwt_token
#     if not token:
#         st.error("Not logged in")
#         return

#     pending = api_get("/sharing/assisted/pending", token=token)

#     if isinstance(pending, dict) and pending.get("error"):
#         st.error(f"API error: {pending.get('error')}")
#         return
#     if not isinstance(pending, list):
#         st.error(f"Unexpected response: {pending}")
#         return
#     if not pending:
#         st.info("No pending assisted requests.")
#         return

#     for req in pending:
#         with st.expander(f"Request from {req.get('recipient_email')} for {req.get('service_url')}"):
#             st.write(f"**Request ID:** {req.get('request_id')}")
#             st.write(f"**Expires:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(req.get('expires_at')))}")
#             if st.button("Approve and assist", key=f"approve_{req.get('request_id')}"):
#                 resp = api_post(f"/sharing/assisted/{req.get('request_id')}/approve", {}, token=token)
#                 if resp.get("assist_login_url"):
#                     st.success("Approved! Opening login page...")
#                     st.markdown(f"[Open login page]({resp['assist_login_url']})")
#                     # Optionally display the request ID for the owner to copy into the extension
#                     st.code(f"Request ID: {req.get('request_id')}", language="text")
#                 else:
#                     st.error(f"Approval failed: {resp.get('detail', resp)}")








def page_owner_assisted_requests():
    st.title("👑 Owner — Assisted Requests")
    st.markdown("Pending requests from recipients. Click 'Approve and assist' to help them log in.")
    token = st.session_state.jwt_token
    if not token:
        st.error("Not logged in")
        return

    pending = api_get("/sharing/assisted/pending", token=token)

    if isinstance(pending, dict) and pending.get("error"):
        st.error(f"API error: {pending.get('error')}")
        return
    if not isinstance(pending, list):
        st.error(f"Unexpected response: {pending}")
        return
    if not pending:
        st.info("No pending assisted requests.")
        return

    for req in pending:
        with st.expander(f"Request from {req.get('recipient_email')} for {req.get('service_url')}"):
            st.write(f"**Request ID:** `{req.get('request_id')}`")
            st.write(f"**Expires:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(req.get('expires_at')))}")
            if st.button("Approve and assist", key=f"approve_{req.get('request_id')}"):
                resp = api_post(f"/sharing/assisted/{req.get('request_id')}/approve", {}, token=token)
                if resp.get("assist_login_url"):
                    st.success("✅ Approved! Login page opened below.")
                    st.markdown(f"🔗 [Open login page]({resp['assist_login_url']})")
                    st.info("📋 **Copy this Request ID for the extension:**")
                    st.code(req.get('request_id'), language="text")
                    st.warning("After manual login, open the extension, paste this Request ID, and click 'Capture and send session'.")
                else:
                    st.error(f"Approval failed: {resp.get('detail', resp)}")









                    






def page_keycloak_device_flow():
    st.title("🔐 Keycloak Passwordless Share (Device Authorization Grant)")
    st.markdown(
        """
This module implements option B: sharing access **without sharing the password**.

**Goal:** Recipient logs in via Keycloak Device Flow, backend performs Relay Login and returns a **handoff session**
for the browser extension to inject cookies/storage — **the password is never shown to the recipient**.
"""
    )

    # ---------- Owner (A) ----------
    st.markdown("---")
    st.subheader("Owner (A) — Generate passwordless handoff link")

    if not st.session_state.get("jwt_token"):
        st.warning("You must be logged in with ZKP (owner session) to generate a handoff link.")
        st.info("Go to 🔐 ZKP Login first.")
    else:
        colA, colB = st.columns([1, 1], gap="large")
        with colA:
            st.caption("Enter the Share ID (SharedAccess.id) you want to hand off.")
            share_id = st.number_input("Share ID", min_value=1, step=1, value=1)

            if st.button("🚀 Generate Keycloak device link", use_container_width=True):
                with st.spinner("Starting device flow on Keycloak..."):
                    # endpoint exists after you add backend/routers/keycloak_handoff.py
                    r = api_post(
                        f"/keycloak-sharing/handoff/start?share_id={int(share_id)}",
                        data={},
                        token=st.session_state.jwt_token,  # Owner JWT (ZKP)
                    )

                if isinstance(r, dict) and r.get("kc_session_id"):
                    st.session_state["kc_handoff"] = r
                    st.success("✅ Link generated. Share it with the recipient.")
                else:
                    st.error("❌ Failed to generate handoff link.")
                    st.json(r)

        with colB:
            st.markdown("**What you share with the recipient**")
            handoff = st.session_state.get("kc_handoff")
            if not handoff:
                st.info("Generate a link first.")
            else:
                df = handoff.get("device_flow", {}) or {}
                st.write("recipient_link:")
                st.code(handoff.get("recipient_link", ""), language="text")

                st.write("verification_uri_complete:")
                st.code(df.get("verification_uri_complete", ""), language="text")

                st.write("user_code:")
                st.code(df.get("user_code", ""), language="text")

                st.caption(f"kc_session_id expires in ~{handoff.get('expires_in', '??')} seconds")

    # ---------- Recipient (B) ----------
    st.markdown("---")
    st.subheader("Recipient (B) — Authorize on Keycloak, then finalize (no password reveal)")

    handoff = st.session_state.get("kc_handoff") or {}
    default_kc_session_id = handoff.get("kc_session_id", "")

    col1, col2 = st.columns([1, 1], gap="large")

    with col1:
        st.markdown("### Step 1 — Open Keycloak device page")
        df = (handoff.get("device_flow") or {}) if handoff else {}

        verification_uri_complete = df.get("verification_uri_complete", "")
        user_code = df.get("user_code", "")

        if verification_uri_complete:
            st.link_button("Open verification link", verification_uri_complete, use_container_width=True)
        if user_code:
            st.code(user_code, language="text")

        st.caption(
            "Recipient logs in on Keycloak and approves. Then proceed to Step 2 (finalize)."
        )

    with col2:
        st.markdown("### Step 2 — Finalize (backend polls Keycloak + runs relay login)")
        kc_session_id = st.text_input("kc_session_id", value=default_kc_session_id)

        if st.button("✅ Finalize and get extension handoff", use_container_width=True):
            if not kc_session_id.strip():
                st.warning("Paste kc_session_id first.")
            else:
                with st.spinner("Finalizing (polling Keycloak + Relay Login)..."):
                    try:
                        resp = requests.post(
                            f"{API_URL}/keycloak-sharing/handoff/finalize/{kc_session_id.strip()}",
                            timeout=140,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            st.session_state["kc_finalize"] = data
                            st.success("✅ Finalized. Now inject via extension.")
                        else:
                            st.error(f"❌ Finalize failed (HTTP {resp.status_code})")
                            try:
                                st.json(resp.json())
                            except Exception:
                                st.write(resp.text)
                    except Exception as e:
                        st.error(f"Request failed: {e}")

    # ---------- Show result for extension injection ----------
    st.markdown("---")
    st.subheader("Browser Extension — Inject session & open connected profile")

    finalize = st.session_state.get("kc_finalize")
    if not finalize:
        st.info("Finalize first. You will receive a handoff session_id for the extension.")
        return

    handoff_info = (finalize or {}).get("handoff") or {}
    session_id = handoff_info.get("session_id")
    current_url = finalize.get("current_url")
    service_url = finalize.get("service_url")

    if not session_id:
        st.error("Finalize response missing handoff.session_id")
        st.json(finalize)
        return

    handoff_url = f"{API_URL}/sharing/handoff/{session_id}"

    st.success("✅ Handoff session ready (one-time, short-lived).")
    st.write("Service URL:", service_url)
    st.write("Current URL (after login):", current_url)

    st.markdown("### Provide this to the Chrome extension")
    st.code(handoff_url, language="text")
    st.caption(f"Expires in ~{handoff_info.get('expires_in', '??')} seconds. One-time consumption.")

    st.warning(
        "Important: /sharing/handoff/{session_id} is one-time. "
        "Do not open it manually in the browser before using the extension."
    )





















def page_register():
    st.title("📝 Zero-Knowledge Registration")
    st.info("Your password will NEVER be sent to the server. Only the ZKP public key is stored.")

    with st.form("register_form"):
        email = st.text_input("Email")
        username = st.text_input("Username")
        password = st.text_input("Master password", type="password")
        password2 = st.text_input("Confirm password", type="password")
        submitted = st.form_submit_button("Register", use_container_width=True)

    if submitted:
        if not all([email, username, password]):
            st.error("All fields are required")
            return
        if password != password2:
            st.error("Passwords do not match")
            return

        with st.spinner("Generating ZKP public key (client side)..."):
            zkp_public_key_hex, zkp_salt_b64 = client_generate_public_key(password)
            master_salt_b64 = base64.b64encode(os.urandom(32)).decode()

        with st.spinner("Sending ZKP public key..."):
            result = api_post("/auth/register", {
                "email": email,
                "username": username,
                "zkp_public_key": zkp_public_key_hex,
                "zkp_salt": zkp_salt_b64,
                "master_salt": master_salt_b64,
            })

        if "user_id" in result:
            st.markdown(
                """
                <div class="zkp-success">
                  ✅ <strong>Registration successful</strong><br/>
                  <span class="zkp-muted">Zero‑Knowledge confirmed: your password never left the client.</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.write("User ID:", result["user_id"])
        else:
            st.error(f"Error: {result.get('detail', result)}")


# def page_credentials():
#     st.title("🔑 My Encrypted Credentials")
#     token = st.session_state.jwt_token
#     if not token:
#         st.error("Not logged in")
#         return

#     creds = api_get("/credentials/", token=token)
#     if isinstance(creds, list):
#         if not creds:
#             st.info("No credentials. Create one!")
#         for c in creds:
#             with st.expander(f"🔒 {c['name']} — {c.get('service_url', '')}"):
#                 col1, col2 = st.columns(2, gap="large")
#                 with col1:
#                     st.write(f"**Type:** {c['credential_type']}")
#                     st.write(f"**Username:** {c.get('username', '—')}")
#                     st.write(f"**Tags:** {c.get('tags', '—')}")
#                 with col2:
#                     st.write(f"**Created:** {time.strftime('%Y-%m-%d %H:%M', time.localtime(c['created_at']))}")
#                     st.write(f"**Active shares:** {c.get('shares_count', 0)}")

#                 if st.button("Decrypt locally", key=f"dec_{c['id']}"):
#                     enc = api_get(f"/credentials/{c['id']}/encrypted", token=token)
#                     if "encrypted_secret" in enc:
#                         try:
#                             secret = client_decrypt(enc["encrypted_secret"], st.session_state.master_password)
#                             st.success(f"Secret: `{secret}`")
#                         except Exception as e:
#                             st.error(f"Decryption failed: {e}")
#     else:
#         st.error(f"API error: {creds}")




def page_credentials():
    st.title("🔑 My Encrypted Credentials")
    token = st.session_state.jwt_token
    if not token:
        st.error("Not logged in")
        return
    


    # Afficher le JWT (pour debug / copier)
    with st.expander("🔐 Your JWT Token (copy for extension or API tests)"):
        st.code(token, language="text")
        st.caption("Ce token est utilisé pour les requêtes authentifiées. Ne le partagez pas.")
    




    creds = api_get("/credentials/", token=token)
    if not isinstance(creds, list):
        st.error(f"API error: {creds}")
        return

    if not creds:
        st.info("No credentials. Create one!")
        return

    for c in creds:
        cred_id = c["id"]

        with st.expander(f"🔒 {c['name']} — {c.get('service_url', '')}"):
            col1, col2 = st.columns(2, gap="large")
            with col1:
                st.write(f"**Type:** {c['credential_type']}")
                st.write(f"**Username:** {c.get('username', '—')}")
                st.write(f"**Tags:** {c.get('tags', '—')}")
            with col2:
                st.write(f"**Created:** {time.strftime('%Y-%m-%d %H:%M', time.localtime(c['created_at']))}")
                st.write(f"**Active shares:** {c.get('shares_count', 0)}")

            # --- Actions row
            a1, a2, a3 = st.columns([1, 1, 2], gap="small")

            with a1:
                if st.button("Decrypt locally", key=f"dec_{cred_id}"):
                    enc = api_get(f"/credentials/{cred_id}/encrypted", token=token)
                    if "encrypted_secret" in enc:
                        try:
                            secret = client_decrypt(enc["encrypted_secret"], st.session_state.master_password)
                            st.success(f"Secret: `{secret}`")
                        except Exception as e:
                            st.error(f"Decryption failed: {e}")
                    else:
                        st.error(f"API error: {enc}")

            with a2:
                # Soft delete (existing endpoint)
                if st.button("Soft delete", key=f"soft_del_{cred_id}"):
                    resp = api_delete(f"/credentials/{cred_id}", token=token)
                    if isinstance(resp, dict) and resp.get("message"):
                        st.success(resp["message"])
                        st.rerun()
                    else:
                        st.error(f"Delete failed: {resp}")

            # Danger zone for hard delete
            st.divider()
            st.subheader("⚠️ Danger zone")

            st.warning("Hard delete is irreversible. It will permanently remove the credential and its shares.")

            confirm = st.checkbox(
                f"I understand. Permanently delete credential #{cred_id}",
                key=f"confirm_hard_{cred_id}",
            )

            if st.button(
                "Hard delete permanently",
                type="primary",
                disabled=not confirm,
                key=f"hard_del_{cred_id}",
            ):
                resp = api_delete(f"/credentials/{cred_id}/hard", token=token)
                if isinstance(resp, dict) and resp.get("message"):
                    st.success(resp["message"])
                    st.rerun()
                else:
                    st.error(f"Hard delete failed: {resp}")








def page_new_credential():
    st.title("➕ New Credential")
    token = st.session_state.jwt_token
    if not token:
        st.error("Not logged in")
        return

    with st.form("new_cred_form"):
        name = st.text_input("Name (e.g., DVWA Admin)")
        service_url = st.text_input("Service URL")
        username = st.text_input("Username/Login")
        secret = st.text_input("Secret (password, API key...)", type="password")
        cred_type = st.selectbox("Type", ["password", "api_key", "token", "certificate"])
        tags = st.text_input("Tags (comma separated)")
        submitted = st.form_submit_button("Save (encrypted)", use_container_width=True)

    if submitted and name and secret:
        with st.spinner("Local AES-256-GCM encryption..."):
            encrypted = client_encrypt(secret, st.session_state.master_password, st.session_state.master_salt)

        with st.spinner("Secure storage..."):
            result = api_post("/credentials/", {
                "name": name,
                "service_url": service_url,
                "username": username,
                "credential_type": cred_type,
                "encrypted_secret": encrypted,
                "tags": tags,
            }, token=token)

        if "id" in result:
            st.markdown(
                f"""
                <div class="zkp-success">
                  ✅ <strong>Credential created</strong> (ID: {result['id']})<br/>
                  <span class="zkp-muted">Secret encrypted locally — the server never saw the plaintext.</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.error(f"Error: {result}")


def page_share():
    st.title("🤝 Zero-Knowledge Secure Sharing")
    st.markdown(
        """
        <div class="zkp-card">
          <strong>Flow</strong><br/>
          1) You decrypt locally<br/>
          2) You re-encrypt with a one-time share token<br/>
          3) Recipient decrypts only client-side<br/>
          4) Token can be revoked / limited by TTL and uses
        </div>
        """,
        unsafe_allow_html=True,
    )

    token = st.session_state.jwt_token
    if not token:
        st.error("Not logged in")
        return

    creds = api_get("/credentials/", token=token)
    if not isinstance(creds, list) or not creds:
        st.info("No credentials to share.")
        return

    cred_options = {f"{c['name']} (ID:{c['id']})": c['id'] for c in creds}

    with st.form("share_form"):
        selected = st.selectbox("Credential to share", list(cred_options.keys()))
        recipient_email = st.text_input("Recipient email")
        secret_to_share = st.text_input(
            "Secret to share (decrypted locally)",
            type="password",
            help="Enter the secret as it will be received by the recipient",
        )
        permission = st.selectbox("Permission", ["read_once", "read"])
        ttl_hours = st.slider("Validity duration (hours)", 1, 168, 24)
        max_uses = st.number_input("Max uses", 1, 10, 1)
        submitted = st.form_submit_button("Create share", use_container_width=True)

    if submitted and recipient_email and secret_to_share:
        cred_id = cred_options[selected]

        with st.spinner("Creating share intent..."):
            intent = api_post("/sharing/create-intent", {
                "credential_id": cred_id,
                "recipient_email": recipient_email,
                "permission": permission,
                "ttl_hours": ttl_hours,
                "max_uses": int(max_uses),
            }, token=token)

        if "share_token" not in intent:
            st.error(f"Intent error: {intent}")
            return

        share_token = intent["share_token"]

        with st.spinner("Local encryption with share token..."):
            plaintext = json.dumps({"password": secret_to_share}, ensure_ascii=False)
            encrypted_payload = encrypt_for_share(plaintext, share_token)

        with st.spinner("Finalizing share..."):
            fin = api_post("/sharing/finalize", {
                "token": share_token,
                "encrypted_payload": encrypted_payload,
            }, token=token)

        if "message" in fin:
            st.markdown(
                """
                <div class="zkp-success">
                  ✅ <strong>Share created and finalized successfully</strong><br/>
                  <span class="zkp-muted">Send the token via a secure channel (Signal, encrypted email, etc.).</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown("### Share token to send")
            st.code(share_token, language="text")
            st.warning(f"Send this token via a secure channel to {recipient_email}")
        else:
            st.error(f"Finalize error: {fin}")









def page_relay_login():
    st.title("🚪 Secure Relay Login (without revealing the password)")
    

    if not st.session_state.get("jwt_token"):
        st.error("You must be logged in (JWT ZKP) to use Relay Login.")
        return

    with st.form("relay_form"):
        token_input = st.text_input("🔑 Sharing token")
        submitted = st.form_submit_button("🔐 Login via Relay", use_container_width=True)

    if submitted and token_input:
        with st.spinner("Relay login..."):
            result = api_post(
                "/sharing/relay-login",
                {"token": token_input},
                token=st.session_state.jwt_token,
            )


        if isinstance(result, dict) and "handoff" in result and result["handoff"].get("session_id"):
            st.success("✅ Relay login successful. Cookies stored server-side (not displayed).")
            st.write("URL after login:", result.get("relay", {}).get("current_url"))
            st.write("Title:", result.get("relay", {}).get("title"))

            session_id = result["handoff"]["session_id"]
            expires_in = result["handoff"].get("expires_in")






            # ... inside the place where you have session_id:
            # handoff_url = f"{API_URL}/sharing/handoff/{session_id}"
            # bridge_url = f"{API_URL}/extension/connect?handoff={urllib.parse.quote(handoff_url, safe='')}"

            # st.link_button("🚀 Open connected profile", bridge_url, use_container_width=True)
            # st.caption("This will trigger the Chrome extension automatically if installed.")





            handoff_url = f"{API_URL}/sharing/handoff/{session_id}"

            st.markdown("### Next Step (Automatic injection via Chrome extension)")
            st.write("Handoff URL (to provide to the extension):")
            st.code(handoff_url, language="text")
            st.caption(f"Expires in ~{expires_in} seconds.")

            
        else:
            st.error(f"❌ Relay login failed: {result.get('detail', result)}")






def page_access_share():
    st.title("📩 Access a Shared Credential (No Secret Reveal)")
    st.markdown(
        """
Enter the received share token.  
We will **verify** that you are allowed to use it **without revealing the secret**.
Then you can use **Relay Login** to log into the target website.
        """
    )

    token = st.session_state.get("jwt_token")
    if not token:
        st.error("You must be logged in to access a share (missing jwt_token).")
        return

    with st.form("access_form"):
        token_input = st.text_input("🔑 Share token")
        submitted = st.form_submit_button("Verify access", use_container_width=True)

    if submitted:
        if not token_input:
            st.warning("Please paste the share token.")
            return

        with st.spinner("Verifying access..."):
            result = api_post("/sharing/access", {"token": token_input}, token=token)

        # ---- SUCCESS PATH (new logic) ----
        if isinstance(result, dict) and result.get("next_action") == "relay_login":
            st.success("✅ Access verified. Secret will NOT be revealed.")
            st.write("Credential name:", result.get("credential_name"))
            st.write("Service URL:", result.get("service_url"))
            st.write("Username:", result.get("username"))
            st.write("Permission:", result.get("permission"))
            st.write("Uses:", f"{result.get('use_count')} / {result.get('max_uses')}")
            st.info(result.get("message"))

            st.markdown("---")
            st.subheader("🚪 Relay Login (no password reveal)")

            requester_email = st.text_input(
                "📧 Your email (must match the recipient email)",
                value=result.get("username") or "",
            )

            if st.button("🔐 Login via Relay", use_container_width=True):
                with st.spinner("Relay login..."):
                    relay = api_post(
                        "/sharing/relay-login",
                        {"token": token_input, "requester_email": requester_email},
                        token=token,
                    )

                if isinstance(relay, dict) and "cookies" in relay:
                    st.success("✅ Relay login OK. Session cookies retrieved.")
                    st.write("URL after login:", relay.get("relay", {}).get("current_url"))
                    st.write("Title:", relay.get("relay", {}).get("title"))
                    st.json(relay.get("cookies", []))
                    st.warning(
                        "Streamlit cannot automatically inject these cookies into your browser. "
                        "Use a browser extension/script to import cookies if you want to browse as logged-in."
                    )

                    st.download_button(
                    "⬇️ Download cookies.json",
                    data=json.dumps(relay.get("cookies", []), indent=2),
                    file_name="cookies.json",
                    mime="application/json",
            )
                    
                else:
                    st.error(f"❌ Relay login failed: {relay.get('detail', relay)}")



                if isinstance(relay, dict) and "handoff" in relay and relay["handoff"].get("session_id"):
                    render_handoff_ui(
                    service_url=relay.get("service_url") or "",
                    session_id=relay["handoff"]["session_id"],
                    )
                else:
                    st.error(f"❌ Relay login failed: {relay.get('detail', relay)}")







            return

        # ---- ERROR PATH ----
        st.error(f"❌ Access denied: {result.get('detail', result)}")






def render_handoff_ui(service_url: str, session_id: str):
    handoff_api_url = f"{API_URL}/sharing/handoff/{session_id}"
    connect_url = make_extension_connect_url(handoff_api_url)

    st.markdown(
        """
        <div class="zkp-card">
          <div style="display:flex; justify-content:space-between; gap:12px; align-items:flex-start;">
            <div style="flex:1;">
              <div class="zkp-badge">BROWSER HANDOFF</div>
              <h3 style="margin:10px 0 6px 0;">Open a connected profile (automatic injection)</h3>
              <div class="zkp-muted">
                The backend prepared a short-lived handoff session. Your extension can fetch cookies + storage
                and open the website already authenticated.
              </div>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns([1.2, 1], gap="large")
    with col1:
        st.success("✅ Relay login OK. Handoff session created.")
        st.write("Target service:", service_url)
        st.caption("Handoff expires quickly. Use it immediately.")

        st.markdown("#### Option A — One click (recommended)")
        st.link_button(
            "🚀 Send to extension & open connected profile",
            connect_url,
            use_container_width=True,
        )
        st.caption("This opens a URL that your extension listens to, then injection runs automatically.")

        st.markdown("#### Option B — Manual (fallback)")
        st.code(handoff_api_url, language="text")

        st.download_button(
            "⬇️ Download handoff url (txt)",
            data=handoff_api_url,
            file_name="handoff_url.txt",
            mime="text/plain",
            use_container_width=True,
        )

    with col2:
        st.info("Extension requirements")
        st.markdown(
            """
- Install the Chrome extension
- Keep it enabled
- Must allow host permissions for:
  - your API (e.g. `http://localhost:8001/*`)
  - target website (e.g. `https://recolyse.com/*`)
            """
        )














# def page_assisted_relay_login():
#     st.title("👥 Owner‑Assisted Relay Login")
#     st.markdown("""
#     Utilisez ce mode si le site cible a un CAPTCHA, une 2FA, ou une procédure de connexion complexe.
#     Le propriétaire sera notifié et devra se connecter manuellement. Sa session vous sera ensuite transmise.
#     """)

#     token_input = st.text_input("🔑 Share token")
#     if st.button("Demander l'accès", use_container_width=True):
#         if not token_input:
#             st.error("Veuillez entrer un token")
#             return
#         with st.spinner("Création de la demande..."):
#             resp = api_post("/sharing/assisted/request", {"share_token": token_input}, token=st.session_state.jwt_token)
#         if "request_id" in resp:
#             st.session_state.assist_request_id = resp["request_id"]
#             st.success(f"Demande créée (ID: {resp['request_id']}). En attente de l'approbation du propriétaire...")
#         else:
#             st.error(f"Erreur: {resp.get('detail', resp)}")

#     if "assist_request_id" in st.session_state:
#         rid = st.session_state.assist_request_id
#         status_resp = api_get(f"/sharing/assisted/{rid}/status", token=st.session_state.jwt_token)
#         if status_resp.get("status") == "completed":
#             handoff_session_id = status_resp["handoff_session_id"]
#             handoff_url = f"{API_URL}/sharing/handoff/{handoff_session_id}"
#             st.success("✅ Le propriétaire a terminé la connexion !")
#             st.markdown("### Handoff URL (à utiliser dans l'extension)")
#             st.code(handoff_url, language="text")
#             st.info("Copiez cette URL dans l'extension Chrome pour injecter la session.")
#         elif status_resp.get("status") == "expired":
#             st.error("La demande a expiré. Veuillez recommencer.")
#         else:
#             st.info(f"Statut: {status_resp.get('status')} (en attente)")



















def page_audit():
    st.title("📋 Audit Trail")
    token = st.session_state.jwt_token
    if not token:
        st.error("Not logged in")
        return

    shares = api_get("/sharing/my-shares", token=token)
    if not isinstance(shares, list):
        st.error("API error")
        return
    if not shares:
        st.info("No active shares.")
        return

    for s in shares:
        status = "✅ Active" if not s.get("is_expired") else "⏰ Expired"
        with st.expander(f"{status} | {s['credential_name']} → {s['recipient_email']}"):
            col1, col2 = st.columns(2, gap="large")
            with col1:
                st.write(f"**Permission:** {s['permission']}")
                st.write(f"**Uses:** {s['use_count']}/{s['max_uses']}")
                st.write(f"**Expires:** {time.strftime('%Y-%m-%d %H:%M', time.localtime(s['expires_at']))}")
            with col2:
                if st.button("View detailed audit", key=f"audit_{s['share_id']}"):
                    audit = api_get(f"/sharing/audit/{s['share_id']}", token=token)
                    st.json(audit)
                if st.button("Revoke", key=f"rev_{s['share_id']}"):
                    res = requests.delete(
                        f"{API_URL}/sharing/revoke/{s['share_id']}",
                        headers={"Authorization": f"Bearer {token}"}
                    ).json()
                    st.success(res.get("message", "Revoked"))
                    st.rerun()


def page_about_zkp():
    st.title("ℹ️ Zero-Knowledge Proof — Explanations")
    st.markdown(
        """
## What is a Zero‑Knowledge Proof (ZKP)?

A **Zero‑Knowledge Proof** allows one party (the **prover**) to convince
another party (the **verifier**) that they know a secret, **without revealing that secret**.

---

## Schnorr Protocol (implemented here)

| Step | Actor | Action |
|------|-------|--------|
| Setup | Alice | Chooses `x` (secret), computes `Y = g^x mod p` (public) |
| 1. Commitment | Alice | Chooses random `r`, sends `Y_r = g^r mod p` |
| 2. Challenge | Server | Computes `c = H(Y ‖ Y_r ‖ email)` |
| 3. Response | Alice | Computes `s = r - c·x mod q`, sends `s` |
| 4. Verify | Server | Checks `g^s · Y^c ≡ Y_r (mod p)` |

### Guaranteed properties:
- **Completeness**: An honest prover always convinces the verifier
- **Soundness**: A dishonest prover cannot cheat the verifier
- **Zero‑Knowledge**: The verifier learns nothing about `x`

---

## Zero‑Trust Architecture

Client Browser → Server → Database  
─────────────  ──────────  ───────────────  
password (local) → Y = g^x mod p → Y (public key)  
AES encrypt(secret) → encrypted blob → encrypted blob  
r (local random) → challenge c → challenge (TTL 5min)  
s = r-cx mod q → verify g^s·Y^c=Y_r → nothing (token invalidated)

---

## Technologies used

| Component | Technology |
|-----------|------------|
| ZKP | Schnorr Protocol (2048‑bit Schnorr group) |
| Encryption | AES-256-GCM (AEAD) |
| Key derivation | PBKDF2-HMAC-SHA256 (310,000 iterations) |
| Auth tokens | JWT HS256 |
| Backend | FastAPI + SQLAlchemy |
| Frontend | Streamlit |
| Architecture | Zero‑Trust |
        """
    )


if __name__ == "__main__":
    main()
