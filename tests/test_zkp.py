"""
Tests unitaires - ZKP Secure Credential Sharing
================================================
Couvre : Schnorr ZKP, chiffrement AES-GCM, token manager.
"""

import base64
import json
import os
import time
import pytest

from backend.crypto.zkp_engine import SchnorrZKP
from backend.crypto.encryption import CredentialEncryptor, ShareEncryptor
from backend.crypto.token_manager import (
    generate_share_token,
    validate_and_consume_token,
    revoke_token,
    list_active_tokens,
)


# ─── Tests ZKP Schnorr ────────────────────────────────────────────────────────

class TestSchnorrZKP:

    def setup_method(self):
        self.zkp = SchnorrZKP()
        self.salt = os.urandom(32)
        self.password = "MonSuperSecret123!"

    def test_public_key_generation(self):
        pub_key, salt = self.zkp.generate_public_key(self.password, self.salt)
        assert pub_key.public_value > 1
        assert pub_key.p == self.zkp.p
        assert pub_key.g == self.zkp.g

    def test_same_password_same_public_key(self):
        pk1, _ = self.zkp.generate_public_key(self.password, self.salt)
        pk2, _ = self.zkp.generate_public_key(self.password, self.salt)
        assert pk1.public_value == pk2.public_value

    def test_different_passwords_different_keys(self):
        pk1, _ = self.zkp.generate_public_key("password1", self.salt)
        pk2, _ = self.zkp.generate_public_key("password2", self.salt)
        assert pk1.public_value != pk2.public_value

    def test_zkp_proof_valid(self):
        pub_key, salt = self.zkp.generate_public_key(self.password, self.salt)
        commitment = self.zkp.create_commitment()
        challenge = self.zkp.generate_challenge(pub_key.public_value, commitment.commitment_value, "test@example.com")
        proof = self.zkp.create_proof(self.password, salt, commitment, challenge)
        assert self.zkp.verify_proof(proof, pub_key, "test@example.com")

    def test_zkp_proof_wrong_password(self):
        pub_key, salt = self.zkp.generate_public_key(self.password, self.salt)
        commitment = self.zkp.create_commitment()
        challenge = self.zkp.generate_challenge(pub_key.public_value, commitment.commitment_value, "test@example.com")
        proof = self.zkp.create_proof("wrong_password", salt, commitment, challenge)
        assert not self.zkp.verify_proof(proof, pub_key, "test@example.com")

    def test_zkp_wrong_context_fails(self):
        pub_key, salt = self.zkp.generate_public_key(self.password, self.salt)
        commitment = self.zkp.create_commitment()
        challenge = self.zkp.generate_challenge(pub_key.public_value, commitment.commitment_value, "user@example.com")
        proof = self.zkp.create_proof(self.password, salt, commitment, challenge)
        # Contexte différent → doit échouer
        assert not self.zkp.verify_proof(proof, pub_key, "attacker@example.com")

    def test_interactive_verify(self):
        pub_key, salt = self.zkp.generate_public_key(self.password, self.salt)
        commitment = self.zkp.create_commitment()
        challenge = self.zkp.generate_challenge(pub_key.public_value, commitment.commitment_value, "")
        proof = self.zkp.create_proof(self.password, salt, commitment, challenge)
        valid = self.zkp.interactive_verify(
            commitment_value=commitment.commitment_value,
            public_key_value=pub_key.public_value,
            response=proof.response,
            challenge=challenge,
        )
        assert valid

    def test_commitment_is_random(self):
        c1 = self.zkp.create_commitment()
        c2 = self.zkp.create_commitment()
        assert c1.commitment_value != c2.commitment_value


# ─── Tests Chiffrement AES-GCM ────────────────────────────────────────────────

class TestCredentialEncryptor:

    def test_encrypt_decrypt_roundtrip(self):
        enc = CredentialEncryptor.encrypt("MonSecret", "master_password")
        assert "salt" in enc and "nonce" in enc and "ciphertext" in enc
        result = CredentialEncryptor.decrypt(enc, "master_password")
        assert result == "MonSecret"

    def test_wrong_password_raises(self):
        enc = CredentialEncryptor.encrypt("MonSecret", "correct_password")
        with pytest.raises(ValueError):
            CredentialEncryptor.decrypt(enc, "wrong_password")

    def test_encrypt_json_roundtrip(self):
        data = {"username": "admin", "password": "p@ss", "url": "http://dvwa"}
        enc_str = CredentialEncryptor.encrypt_json(data, "master")
        result = CredentialEncryptor.decrypt_json(enc_str, "master")
        assert result == data

    def test_different_nonces(self):
        enc1 = CredentialEncryptor.encrypt("secret", "pass")
        enc2 = CredentialEncryptor.encrypt("secret", "pass")
        assert enc1["nonce"] != enc2["nonce"]


class TestShareEncryptor:

    def test_share_roundtrip(self):
        token, raw_key = ShareEncryptor.generate_share_key()
        enc = ShareEncryptor.encrypt_for_share("MyPassword123", raw_key)
        result = ShareEncryptor.decrypt_from_share(enc, token)
        assert result == "MyPassword123"

    def test_wrong_token_raises(self):
        token, raw_key = ShareEncryptor.generate_share_key()
        enc = ShareEncryptor.encrypt_for_share("secret", raw_key)
        wrong_token = base64.urlsafe_b64encode(os.urandom(32)).decode()
        with pytest.raises(ValueError):
            ShareEncryptor.decrypt_from_share(enc, wrong_token)


# ─── Tests Token Manager ─────────────────────────────────────────────────────

class TestTokenManager:

    def test_generate_and_validate_token(self):
        token = generate_share_token(
            credential_id=1, owner_id=42,
            recipient_email="bob@example.com",
            permission="read_once", ttl_hours=1, max_uses=1
        )
        assert token is not None
        share = validate_and_consume_token(token, "bob@example.com")
        assert share is not None
        assert share.credential_id == 1
        assert share.owner_id == 42

    def test_wrong_email_rejected(self):
        token = generate_share_token(
            credential_id=2, owner_id=1,
            recipient_email="alice@example.com",
            ttl_hours=1
        )
        share = validate_and_consume_token(token, "eve@example.com")
        assert share is None

    def test_one_time_token_exhausted(self):
        token = generate_share_token(
            credential_id=3, owner_id=1,
            recipient_email="bob@example.com",
            permission="read_once", max_uses=1
        )
        share1 = validate_and_consume_token(token, "bob@example.com")
        share2 = validate_and_consume_token(token, "bob@example.com")
        assert share1 is not None
        assert share2 is None  # Token épuisé

    def test_revoke_token(self):
        token = generate_share_token(
            credential_id=4, owner_id=99,
            recipient_email="carol@example.com",
            ttl_hours=24
        )
        revoked = revoke_token(token, owner_id=99)
        assert revoked
        share = validate_and_consume_token(token, "carol@example.com")
        assert share is None  # Token révoqué

    def test_list_active_tokens(self):
        generate_share_token(5, 77, "dave@example.com", ttl_hours=1)
        tokens = list_active_tokens(77)
        assert len(tokens) >= 1
        assert "credential_id" in tokens[0]
