"""
Encryption Module - AES-256-GCM + PBKDF2
=========================================
Chiffrement des credentials stockés en base de données.
- AES-256-GCM : chiffrement authentifié (confidentialité + intégrité)
- PBKDF2-HMAC-SHA256 : dérivation de clé depuis le mot de passe maître
- Les credentials ne sont JAMAIS stockés en clair.
"""

import base64
import hashlib
import json
import os
import secrets
from typing import Any, Dict, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from backend.crypto.b64utils import b64url_decode_padded


PBKDF2_ITERATIONS = 310_000
KEY_LENGTH = 32       # 256 bits
NONCE_LENGTH = 12     # 96 bits (recommandé GCM)
SALT_LENGTH = 32      # 256 bits


def _b64decode_urlsafe_padded(s: str) -> bytes:
    """
    Décode base64 urlsafe avec padding correct.
    IMPORTANT: ne pas faire token + '==' (padding incorrect possible).
    """
    s = (s or "").strip()
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("utf-8"))


class CredentialEncryptor:
    """
    Chiffrement/déchiffrement des credentials avec AES-256-GCM.
    La clé est dérivée du mot de passe master de l'utilisateur.
    """

    @staticmethod
    def derive_key(master_password: str, salt: bytes) -> bytes:
        """Dérive une clé AES-256 depuis le mot de passe master."""
        return hashlib.pbkdf2_hmac(
            "sha256",
            master_password.encode("utf-8"),
            salt,
            iterations=PBKDF2_ITERATIONS,
            dklen=KEY_LENGTH,
        )

    @staticmethod
    def encrypt(plaintext: str, master_password: str) -> Dict[str, str]:
        """
        Chiffre un secret avec AES-256-GCM.
        Retourne un dict JSON-sérialisable contenant salt, nonce, ciphertext.
        """
        salt = os.urandom(SALT_LENGTH)
        nonce = os.urandom(NONCE_LENGTH)
        key = CredentialEncryptor.derive_key(master_password, salt)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return {
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }

    @staticmethod
    def decrypt(encrypted_data: Dict[str, str], master_password: str) -> str:
        """
        Déchiffre un secret AES-256-GCM.
        Lève ValueError si le mot de passe est incorrect (tag GCM invalide).
        """
        salt = base64.b64decode(encrypted_data["salt"])
        nonce = base64.b64decode(encrypted_data["nonce"])
        ciphertext = base64.b64decode(encrypted_data["ciphertext"])
        key = CredentialEncryptor.derive_key(master_password, salt)
        aesgcm = AESGCM(key)
        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
            return plaintext.decode("utf-8")
        except Exception:
            raise ValueError("Déchiffrement échoué : mot de passe incorrect ou données corrompues.")

    @staticmethod
    def encrypt_json(data: Dict[str, Any], master_password: str) -> str:
        """Chiffre un dictionnaire → retourne une chaîne base64 JSON."""
        plaintext = json.dumps(data, ensure_ascii=False)
        result = CredentialEncryptor.encrypt(plaintext, master_password)
        return json.dumps(result)

    @staticmethod
    def decrypt_json(encrypted_str: str, master_password: str) -> Dict[str, Any]:
        """Déchiffre une chaîne produite par encrypt_json."""
        encrypted_data = json.loads(encrypted_str)
        plaintext = CredentialEncryptor.decrypt(encrypted_data, master_password)
        return json.loads(plaintext)


class ShareEncryptor:
    """
    Chiffrement pour le partage de credentials :
    génère une clé éphémère (one-time) pour chaque partage.
    Zero-knowledge : ni le serveur ni le destinataire ne connaissent le password original.
    """

    @staticmethod
    def generate_share_key() -> Tuple[str, bytes]:
        """
        Génère une clé de partage éphémère.
        Retourne (token_b64, raw_key_bytes).
        """
        raw_key = secrets.token_bytes(KEY_LENGTH)
        token = base64.urlsafe_b64encode(raw_key).decode()
        return token, raw_key

    @staticmethod
    def encrypt_for_share(plaintext: str, raw_key: bytes) -> Dict[str, str]:
        """Chiffre un secret avec la clé éphémère."""
        nonce = os.urandom(NONCE_LENGTH)
        aesgcm = AESGCM(raw_key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return {
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }

    # @staticmethod
    # def decrypt_from_share(encrypted_data: Dict[str, str], token: str) -> str:
    #     """
    #     Déchiffre un secret partagé avec le token (base64 urlsafe).
    #     Fix: padding base64 correct + vérif longueurs.
    #     """
    #     raw_key = _b64decode_urlsafe_padded(token)
    #     key = raw_key[:KEY_LENGTH]
    #     if len(key) != KEY_LENGTH:
    #         raise ValueError("Token de partage invalide (clé dérivée incorrecte).")

    #     nonce = base64.b64decode(encrypted_data["nonce"])
    #     if len(nonce) != NONCE_LENGTH:
    #         raise ValueError(f"Nonce invalide: {len(nonce)} bytes (attendu {NONCE_LENGTH}).")

    #     ciphertext = base64.b64decode(encrypted_data["ciphertext"])
    #     aesgcm = AESGCM(key)

    #     try:
    #         return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    #     except Exception as e:
    #         # cryptography.exceptions.InvalidTag est le cas typique
    #         raise ValueError(f"Token de partage invalide ou données corrompues: {type(e).__name__}")



    @staticmethod
    def decrypt_from_share(encrypted_data: Dict[str, str], token: str) -> str:
        """
        Déchiffre un secret partagé avec le token.
        Fix: padding base64 correct (ne jamais faire token + '==').
        """
        raw_key = b64url_decode_padded(token)
        key = raw_key[:KEY_LENGTH]
        if len(key) != KEY_LENGTH:
            raise ValueError("Token de partage invalide (clé incorrecte).")

        nonce = base64.b64decode(encrypted_data["nonce"])
        if len(nonce) != NONCE_LENGTH:
            raise ValueError("Nonce invalide.")

        ciphertext = base64.b64decode(encrypted_data["ciphertext"])
        aesgcm = AESGCM(key)
        try:
            return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
        except Exception as e:
            raise ValueError(f"Token invalide ou données corrompues: {type(e).__name__}")
























# """
# Encryption Module - AES-256-GCM + PBKDF2
# =========================================
# Chiffrement des credentials stockés en base de données.
# - AES-256-GCM : chiffrement authentifié (confidentialité + intégrité)
# - PBKDF2-HMAC-SHA256 : dérivation de clé depuis le mot de passe maître
# - Les credentials ne sont JAMAIS stockés en clair.
# """

# import base64
# import hashlib
# import json
# import os
# import secrets
# from typing import Any, Dict, Tuple

# from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# PBKDF2_ITERATIONS = 310_000
# KEY_LENGTH = 32       # 256 bits
# NONCE_LENGTH = 12     # 96 bits (recommandé GCM)
# SALT_LENGTH = 32      # 256 bits


# class CredentialEncryptor:
#     """
#     Chiffrement/déchiffrement des credentials avec AES-256-GCM.
#     La clé est dérivée du mot de passe master de l'utilisateur.
#     """

#     @staticmethod
#     def derive_key(master_password: str, salt: bytes) -> bytes:
#         """Dérive une clé AES-256 depuis le mot de passe master."""
#         return hashlib.pbkdf2_hmac(
#             "sha256",
#             master_password.encode("utf-8"),
#             salt,
#             iterations=PBKDF2_ITERATIONS,
#             dklen=KEY_LENGTH,
#         )

#     @staticmethod
#     def encrypt(plaintext: str, master_password: str) -> Dict[str, str]:
#         """
#         Chiffre un secret avec AES-256-GCM.
#         Retourne un dict JSON-sérialisable contenant salt, nonce, ciphertext.
#         """
#         salt = os.urandom(SALT_LENGTH)
#         nonce = os.urandom(NONCE_LENGTH)
#         key = CredentialEncryptor.derive_key(master_password, salt)
#         aesgcm = AESGCM(key)
#         ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
#         return {
#             "salt": base64.b64encode(salt).decode(),
#             "nonce": base64.b64encode(nonce).decode(),
#             "ciphertext": base64.b64encode(ciphertext).decode(),
#         }

#     @staticmethod
#     def decrypt(encrypted_data: Dict[str, str], master_password: str) -> str:
#         """
#         Déchiffre un secret AES-256-GCM.
#         Lève ValueError si le mot de passe est incorrect (tag GCM invalide).
#         """
#         salt = base64.b64decode(encrypted_data["salt"])
#         nonce = base64.b64decode(encrypted_data["nonce"])
#         ciphertext = base64.b64decode(encrypted_data["ciphertext"])
#         key = CredentialEncryptor.derive_key(master_password, salt)
#         aesgcm = AESGCM(key)
#         try:
#             plaintext = aesgcm.decrypt(nonce, ciphertext, None)
#             return plaintext.decode("utf-8")
#         except Exception:
#             raise ValueError("Déchiffrement échoué : mot de passe incorrect ou données corrompues.")

#     @staticmethod
#     def encrypt_json(data: Dict[str, Any], master_password: str) -> str:
#         """Chiffre un dictionnaire → retourne une chaîne base64 JSON."""
#         plaintext = json.dumps(data, ensure_ascii=False)
#         result = CredentialEncryptor.encrypt(plaintext, master_password)
#         return json.dumps(result)

#     @staticmethod
#     def decrypt_json(encrypted_str: str, master_password: str) -> Dict[str, Any]:
#         """Déchiffre une chaîne produite par encrypt_json."""
#         encrypted_data = json.loads(encrypted_str)
#         plaintext = CredentialEncryptor.decrypt(encrypted_data, master_password)
#         return json.loads(plaintext)


# class ShareEncryptor:
#     """
#     Chiffrement pour le partage de credentials :
#     génère une clé éphémère (one-time) pour chaque partage.
#     Zero-knowledge : ni le serveur ni le destinataire ne connaissent le password original.
#     """

#     @staticmethod
#     def generate_share_key() -> Tuple[str, bytes]:
#         """
#         Génère une clé de partage éphémère.
#         Retourne (token_b64, raw_key_bytes).
#         """
#         raw_key = secrets.token_bytes(KEY_LENGTH)
#         token = base64.urlsafe_b64encode(raw_key).decode()
#         return token, raw_key

#     @staticmethod
#     def encrypt_for_share(plaintext: str, raw_key: bytes) -> Dict[str, str]:
#         """Chiffre un secret avec la clé éphémère."""
#         nonce = os.urandom(NONCE_LENGTH)
#         aesgcm = AESGCM(raw_key)
#         ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
#         return {
#             "nonce": base64.b64encode(nonce).decode(),
#             "ciphertext": base64.b64encode(ciphertext).decode(),
#         }

#     @staticmethod
#     def decrypt_from_share(encrypted_data: Dict[str, str], token: str) -> str:
#         """Déchiffre un secret partagé avec le token."""
#         raw_key = base64.urlsafe_b64decode(token + "==")
#         # token_padded = token + '=' * (-len(token) % 4)
#         # raw_key = base64.urlsafe_b64decode(token_padded)
#         nonce = base64.b64decode(encrypted_data["nonce"])
#         ciphertext = base64.b64decode(encrypted_data["ciphertext"])
#         aesgcm = AESGCM(raw_key[:KEY_LENGTH])
#         try:
#             return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
#         except Exception:
#             raise ValueError("Token de partage invalide ou expiré.")