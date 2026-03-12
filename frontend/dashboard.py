"""
ZKP Secure Credential Sharing - Dashboard Streamlit
=====================================================
Interface utilisateur complète pour la Partie 2.
Implémente le côté CLIENT du protocole ZKP :
  - Dérivation du secret x côté navigateur
  - Génération de l'engagement g^r mod p
  - Calcul de la réponse s = r - c*x mod q
  - Chiffrement/déchiffrement local AES-256-GCM
"""

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Optional

import requests
import streamlit as st
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ─── Configuration ────────────────────────────────────────────────────────────

API_URL = os.getenv("ZKP_API_URL", "http://localhost:8001")

# Paramètres ZKP (identiques au serveur)
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


# ─── Fonctions ZKP Côté Client ────────────────────────────────────────────────

def client_derive_secret(password: str, salt_b64: str) -> int:
    """Dérive le secret x depuis le mot de passe (côté client)."""
    salt = base64.b64decode(salt_b64)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations=310_000, dklen=32)
    return int.from_bytes(dk, "big") % (Q - 1) + 1


def client_generate_public_key(password: str) -> tuple:
    """Génère (Y=g^x mod p, salt_b64)."""
    salt = os.urandom(32)
    x = client_derive_secret(password, base64.b64encode(salt).decode())
    Y = pow(G, x, P)
    return hex(Y), base64.b64encode(salt).decode()


def client_create_commitment() -> tuple:
    """Génère (Y_r=g^r mod p, r)."""
    r = secrets.randbelow(Q - 1) + 1
    Y_r = pow(G, r, P)
    return hex(Y_r), r


def client_compute_response(password: str, salt_b64: str, r: int, challenge_hex: str) -> str:
    """Calcule s = r - c*x mod q."""
    x = client_derive_secret(password, salt_b64)
    c = int(challenge_hex, 16)
    s = (r - c * x) % Q
    return hex(s)


def client_encrypt(plaintext: str, password: str, salt_b64: str) -> str:
    """Chiffre localement avec AES-256-GCM (clé dérivée du password master)."""
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
    """Déchiffre localement avec AES-256-GCM."""
    d = json.loads(encrypted_json)
    salt = base64.b64decode(d["salt"])
    nonce = base64.b64decode(d["nonce"])
    ct = base64.b64decode(d["ciphertext"])
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations=310_000, dklen=32)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()


def encrypt_for_share(plaintext: str, share_token: str) -> str:
    """Chiffre un secret avec un token de partage éphémère."""
    raw_key = base64.urlsafe_b64decode(share_token + "==")[:32]
    nonce = os.urandom(12)
    aesgcm = AESGCM(raw_key)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return json.dumps({
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ct).decode(),
    })


def decrypt_from_share(encrypted_json: str, share_token: str) -> str:
    """Déchiffre avec le token de partage."""
    d = json.loads(encrypted_json)
    raw_key = base64.urlsafe_b64decode(share_token + "==")[:32]
    nonce = base64.b64decode(d["nonce"])
    ct = base64.b64decode(d["ciphertext"])
    aesgcm = AESGCM(raw_key)
    return aesgcm.decrypt(nonce, ct, None).decode()


# ─── API Helpers ──────────────────────────────────────────────────────────────

def api_post(endpoint: str, data: dict, token: str = None) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(f"{API_URL}{endpoint}", json=data, headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def api_get(endpoint: str, token: str = None) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(f"{API_URL}{endpoint}", headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ─── Streamlit UI ─────────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="🔐 ZKP Credential Sharing",
        page_icon="🔐",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # CSS personnalisé
    st.markdown("""
    <style>
    .zkp-badge { background: linear-gradient(135deg,#1a1a2e,#16213e);
                 color:#00d4ff; padding:4px 12px; border-radius:20px;
                 font-size:12px; font-weight:bold; border:1px solid #00d4ff; }
    .success-box { background:#0a3d2e; border-left:4px solid #00ff88;
                   padding:12px; border-radius:4px; margin:8px 0; }
    .warning-box { background:#3d2e0a; border-left:4px solid #ffaa00;
                   padding:12px; border-radius:4px; margin:8px 0; }
    </style>
    """, unsafe_allow_html=True)

    # ─── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## 🔐 ZKP Credential Sharing")
        st.markdown('<span class="zkp-badge">Zero-Knowledge Proof</span>', unsafe_allow_html=True)
        st.markdown("---")

        if "jwt_token" not in st.session_state:
            st.session_state.jwt_token = None
            st.session_state.current_user = None
            st.session_state.master_password = None
            st.session_state.zkp_salt = None
            st.session_state.master_salt = None

        if st.session_state.jwt_token:
            st.success(f"✅ Connecté : {st.session_state.current_user}")
            if st.button("🚪 Déconnexion"):
                for k in ["jwt_token", "current_user", "master_password", "zkp_salt", "master_salt"]:
                    st.session_state[k] = None
                st.rerun()
            menu = st.radio("Navigation", [
                "🔑 Mes Credentials",
                "➕ Nouveau Credential",
                "🤝 Partager",
                "📩 Accéder à un Partage",
                "📋 Audit Trail",
                "ℹ️ À propos du ZKP",
            ])
        else:
            menu = st.radio("Navigation", ["🔐 Connexion ZKP", "📝 Inscription", "ℹ️ À propos du ZKP"])

    # ─── Pages ────────────────────────────────────────────────────────────────

    if menu == "🔐 Connexion ZKP":
        page_login()
    elif menu == "📝 Inscription":
        page_register()
    elif menu == "🔑 Mes Credentials":
        page_credentials()
    elif menu == "➕ Nouveau Credential":
        page_new_credential()
    elif menu == "🤝 Partager":
        page_share()
    elif menu == "📩 Accéder à un Partage":
        page_access_share()
    elif menu == "📋 Audit Trail":
        page_audit()
    elif menu == "ℹ️ À propos du ZKP":
        page_about_zkp()


def page_login():
    st.title("🔐 Connexion Zero-Knowledge Proof")
    st.markdown("""
    > **Principe ZKP** : Vous prouvez que vous connaissez votre mot de passe
    > **sans jamais l'envoyer** au serveur. Le protocole de Schnorr garantit
    > qu'aucun adversaire interceptant les communications ne peut récupérer votre secret.
    """)

    col1, col2 = st.columns([1, 1])
    with col1:
        st.markdown("### Étapes du protocole Schnorr")
        st.markdown("""
        1. 🎲 **Engagement** : Vous générez `r` aléatoire → envoyez `g^r mod p`
        2. 🎯 **Challenge** : Le serveur génère `c = H(Y || g^r || email)`
        3. 📐 **Réponse** : Vous calculez `s = r - c·x mod q`
        4. ✅ **Vérification** : Serveur vérifie `g^s · Y^c ≡ g^r (mod p)`
        """)

    with col2:
        with st.form("login_form"):
            email = st.text_input("📧 Email")
            password = st.text_input("🔑 Mot de passe (reste local)", type="password")
            submitted = st.form_submit_button("🚀 Connexion ZKP", use_container_width=True)

        if submitted and email and password:
            with st.spinner("Récupération des salts..."):
                salts = api_get(f"/auth/salts/{email}")
            if "error" in salts or "zkp_salt" not in salts:
                st.error("❌ Utilisateur non trouvé")
                return

            zkp_salt = salts["zkp_salt"]
            master_salt = salts["master_salt"]

            with st.spinner("Génération de l'engagement ZKP..."):
                commitment_hex, r = client_create_commitment()
                resp_challenge = api_post("/auth/challenge", {
                    "email": email,
                    "commitment": commitment_hex,
                })

            if "error" in resp_challenge or "challenge_id" not in resp_challenge:
                st.error(f"❌ Erreur challenge : {resp_challenge}")
                return

            challenge_id = resp_challenge["challenge_id"]
            challenge_hex = resp_challenge["challenge_value"]

            with st.spinner("Calcul de la preuve ZKP..."):
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
                st.success(f"✅ Connexion ZKP réussie ! Bienvenue {resp_verify['username']}")
                st.balloons()
                st.rerun()
            else:
                st.error(f"❌ Authentification échouée : {resp_verify.get('detail', resp_verify)}")


def page_register():
    st.title("📝 Inscription Zero-Knowledge")
    st.info("🔐 Votre mot de passe ne sera JAMAIS envoyé au serveur. Seule la clé publique ZKP sera stockée.")

    with st.form("register_form"):
        email = st.text_input("📧 Email")
        username = st.text_input("👤 Username")
        password = st.text_input("🔑 Mot de passe master", type="password")
        password2 = st.text_input("🔑 Confirmer le mot de passe", type="password")
        submitted = st.form_submit_button("📝 S'inscrire", use_container_width=True)

    if submitted:
        if not all([email, username, password]):
            st.error("Tous les champs sont requis")
            return
        if password != password2:
            st.error("Les mots de passe ne correspondent pas")
            return

        with st.spinner("Génération de la clé publique ZKP (côté client)..."):
            zkp_public_key_hex, zkp_salt_b64 = client_generate_public_key(password)
            master_salt_b64 = base64.b64encode(os.urandom(32)).decode()

        with st.spinner("Envoi de la clé publique ZKP..."):
            result = api_post("/auth/register", {
                "email": email,
                "username": username,
                "zkp_public_key": zkp_public_key_hex,
                "zkp_salt": zkp_salt_b64,
                "master_salt": master_salt_b64,
            })

        if "user_id" in result:
            st.success(f"✅ Inscription réussie ! ID: {result['user_id']}")
            st.markdown("""
            <div class="success-box">
            ✅ <strong>Zero-Knowledge confirmé</strong> : Votre mot de passe n'a jamais quitté votre navigateur.
            Le serveur stocke uniquement Y = g^x mod p (votre clé publique ZKP).
            </div>
            """, unsafe_allow_html=True)
        else:
            st.error(f"❌ Erreur : {result.get('detail', result)}")


def page_credentials():
    st.title("🔑 Mes Credentials Chiffrés")
    token = st.session_state.jwt_token
    if not token:
        st.error("Non connecté")
        return

    creds = api_get("/credentials/", token=token)
    if isinstance(creds, list):
        if not creds:
            st.info("Aucun credential. Créez-en un !")
        for c in creds:
            with st.expander(f"🔒 {c['name']} — {c.get('service_url', '')}"):
                col1, col2 = st.columns(2)
                with col1:
                    st.write(f"**Type :** {c['credential_type']}")
                    st.write(f"**Username :** {c.get('username', '—')}")
                    st.write(f"**Tags :** {c.get('tags', '—')}")
                with col2:
                    st.write(f"**Créé :** {time.strftime('%Y-%m-%d %H:%M', time.localtime(c['created_at']))}")
                    st.write(f"**Partages actifs :** {c.get('shares_count', 0)}")

                if st.button(f"🔓 Déchiffrer localement", key=f"dec_{c['id']}"):
                    enc = api_get(f"/credentials/{c['id']}/encrypted", token=token)
                    if "encrypted_secret" in enc:
                        try:
                            secret = client_decrypt(enc["encrypted_secret"], st.session_state.master_password)
                            st.success(f"🔓 Secret : `{secret}`")
                        except Exception as e:
                            st.error(f"Déchiffrement échoué : {e}")
    else:
        st.error(f"Erreur API : {creds}")


def page_new_credential():
    st.title("➕ Nouveau Credential")
    token = st.session_state.jwt_token
    if not token:
        st.error("Non connecté")
        return

    with st.form("new_cred_form"):
        name = st.text_input("📛 Nom (ex: DVWA Admin)")
        service_url = st.text_input("🌐 URL du service")
        username = st.text_input("👤 Username/Login")
        secret = st.text_input("🔑 Secret (mot de passe, API key...)", type="password")
        cred_type = st.selectbox("Type", ["password", "api_key", "token", "certificate"])
        tags = st.text_input("🏷️ Tags (séparés par virgules)")
        submitted = st.form_submit_button("💾 Enregistrer (chiffré)", use_container_width=True)

    if submitted and name and secret:
        with st.spinner("Chiffrement local AES-256-GCM..."):
            encrypted = client_encrypt(secret, st.session_state.master_password, st.session_state.master_salt)

        with st.spinner("Enregistrement sécurisé..."):
            result = api_post("/credentials/", {
                "name": name,
                "service_url": service_url,
                "username": username,
                "credential_type": cred_type,
                "encrypted_secret": encrypted,
                "tags": tags,
            }, token=token)

        if "id" in result:
            st.success(f"✅ Credential créé (ID: {result['id']}) — Secret chiffré, serveur ne l'a jamais vu.")
        else:
            st.error(f"❌ Erreur : {result}")


def page_share():
    st.title("🤝 Partage Sécurisé Zero-Knowledge")
    token = st.session_state.jwt_token
    if not token:
        st.error("Non connecté")
        return

    st.markdown("""
    > **Comment ça marche ?**
    > 1. Vous déchiffrez le credential localement
    > 2. Vous re-chiffrez avec une clé éphémère (one-time key)
    > 3. Un token sécurisé est généré — seul le destinataire peut l'utiliser
    > 4. Après usage, le token est automatiquement invalidé
    """)

    creds = api_get("/credentials/", token=token)
    if not isinstance(creds, list) or not creds:
        st.info("Aucun credential à partager.")
        return

    cred_options = {f"{c['name']} (ID:{c['id']})": c['id'] for c in creds}

    with st.form("share_form"):
        selected = st.selectbox("🔒 Credential à partager", list(cred_options.keys()))
        recipient_email = st.text_input("📧 Email du destinataire")
        secret_to_share = st.text_input("🔑 Secret à partager (déchiffré localement)", type="password",
                                         help="Entrez le secret tel qu'il sera reçu par le destinataire")
        permission = st.selectbox("Permission", ["read_once", "read"])
        ttl_hours = st.slider("⏱️ Durée de validité (heures)", 1, 168, 24)
        max_uses = st.number_input("Nombre max d'utilisations", 1, 10, 1)
        submitted = st.form_submit_button("🤝 Créer le partage", use_container_width=True)

    if submitted and recipient_email and secret_to_share:
        cred_id = cred_options[selected]

        with st.spinner("Génération de la clé éphémère..."):
            ephemeral_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
            encrypted_payload = encrypt_for_share(secret_to_share, ephemeral_key)

        with st.spinner("Création du partage sécurisé..."):
            result = api_post("/sharing/create", {
                "credential_id": cred_id,
                "recipient_email": recipient_email,
                "permission": permission,
                "ttl_hours": ttl_hours,
                "max_uses": int(max_uses),
                "encrypted_payload": encrypted_payload,
                "share_key_token": ephemeral_key,
            }, token=token)

        if "share_token" in result:
            st.success("✅ Partage créé avec succès !")
            st.markdown("### 🔑 Token à envoyer au destinataire")
            st.code(result["share_token"], language="text")
            st.warning(f"⚠️ Envoyez ce token par un canal sécurisé (Signal, email chiffré, etc.) à {recipient_email}")
            st.info(f"⏱️ Expire dans {ttl_hours}h | Utilisations max: {max_uses}")
        else:
            st.error(f"❌ Erreur : {result}")


def page_access_share():
    st.title("📩 Accéder à un Credential Partagé")
    st.markdown("""
    > Entrez le token de partage reçu. Le secret vous sera transmis chiffré
    > et sera déchiffré **uniquement dans votre navigateur**.
    """)

    with st.form("access_form"):
        token_input = st.text_input("🔑 Token de partage", help="Token reçu du propriétaire")
        requester_email = st.text_input("📧 Votre email")
        submitted = st.form_submit_button("🔓 Accéder", use_container_width=True)

    if submitted and token_input and requester_email:
        with st.spinner("Vérification Zero-Trust..."):
            result = api_post("/sharing/access", {
                "token": token_input,
                "requester_email": requester_email,
            })

        if "encrypted_payload" in result:
            try:
                decrypted_secret = decrypt_from_share(result["encrypted_payload"], result["decryption_key"])
                st.success(f"✅ Accès autorisé — Credential : **{result['credential_name']}**")
                st.markdown(f"""
                <div class="success-box">
                <strong>Service :</strong> {result.get('service_url', '—')}<br>
                <strong>Username :</strong> {result.get('username', '—')}<br>
                <strong>🔓 Secret :</strong> <code>{decrypted_secret}</code>
                </div>
                """, unsafe_allow_html=True)
                if result.get("permission") == "read_once":
                    st.warning("⚠️ Token à usage unique — invalidé après cet accès")
            except Exception as e:
                st.error(f"❌ Déchiffrement local échoué : {e}")
        else:
            st.error(f"❌ Accès refusé : {result.get('detail', result)}")


def page_audit():
    st.title("📋 Audit Trail")
    token = st.session_state.jwt_token
    if not token:
        st.error("Non connecté")
        return

    shares = api_get("/sharing/my-shares", token=token)
    if not isinstance(shares, list):
        st.error("Erreur API")
        return
    if not shares:
        st.info("Aucun partage actif.")
        return

    for s in shares:
        status = "✅ Actif" if not s.get("is_expired") else "⏰ Expiré"
        with st.expander(f"{status} | {s['credential_name']} → {s['recipient_email']}"):
            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Permission :** {s['permission']}")
                st.write(f"**Utilisations :** {s['use_count']}/{s['max_uses']}")
                st.write(f"**Expire :** {time.strftime('%Y-%m-%d %H:%M', time.localtime(s['expires_at']))}")
            with col2:
                if st.button("📋 Voir audit détaillé", key=f"audit_{s['share_id']}"):
                    audit = api_get(f"/sharing/audit/{s['share_id']}", token=token)
                    st.json(audit)
                if st.button("🚫 Révoquer", key=f"rev_{s['share_id']}"):
                    res = requests.delete(
                        f"{API_URL}/sharing/revoke/{s['share_id']}",
                        headers={"Authorization": f"Bearer {token}"}
                    ).json()
                    st.success(res.get("message", "Révoqué"))
                    st.rerun()


def page_about_zkp():
    st.title("ℹ️ Zero-Knowledge Proof — Explications")
    st.markdown("""
    ## Qu'est-ce qu'une Preuve à Divulgation Nulle (ZKP) ?

    Une **Zero-Knowledge Proof** permet à une partie (le **prouveur**) de convaincre
    une autre partie (le **vérificateur**) qu'elle connaît un secret, **sans révéler ce secret**.

    ---

    ## Le Protocole de Schnorr (implémenté ici)

    | Étape | Acteur | Action |
    |-------|--------|--------|
    | Setup | Alice | Choisit `x` (secret), calcule `Y = g^x mod p` (public) |
    | 1. Commitment | Alice | Choisit `r` aléatoire, envoie `Y_r = g^r mod p` |
    | 2. Challenge | Serveur | Calcule `c = H(Y ‖ Y_r ‖ email)` |
    | 3. Response | Alice | Calcule `s = r - c·x mod q`, envoie `s` |
    | 4. Verify | Serveur | Vérifie `g^s · Y^c ≡ Y_r (mod p)` |

    ### Propriétés garanties :
    - **Complétude** : Un prouveur honnête convainc toujours le vérificateur
    - **Solidité** : Un prouveur malhonnête ne peut pas tromper le vérificateur
    - **Zero-Knowledge** : Le vérificateur n'apprend rien sur `x`

    ---

    ## Architecture Zero-Trust

    ```
    Client Browser          Serveur                  Base de données
    ─────────────          ──────────                ───────────────
    password (local)    →  Y = g^x mod p         →  Y (clé publique)
    AES encrypt(secret) →  encrypted blob         →  blob chiffré
    r (aléatoire local) →  challenge c            →  challenge (TTL 5min)
    s = r-cx mod q      →  vérif g^s·Y^c=Y_r     →  rien (token invalidé)
    ```

    ### Principe du moindre privilège :
    - Chaque credential a ses propres clés
    - Les tokens de partage sont one-time use
    - Audit complet de chaque accès
    - Révocation instantanée

    ---

    ## Technologies utilisées

    | Composant | Technologie |
    |-----------|-------------|
    | ZKP | Schnorr Protocol (groupe de Schnorr 2048-bit) |
    | Chiffrement | AES-256-GCM (AEAD) |
    | Dérivation clé | PBKDF2-HMAC-SHA256 (310,000 itérations) |
    | Auth tokens | JWT HS256 |
    | Backend | FastAPI + SQLAlchemy |
    | Frontend | Streamlit |
    | Architecture | Zero-Trust |
    """)


if __name__ == "__main__":
    main()