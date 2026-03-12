"""
Zero-Knowledge Proof Engine - Schnorr Protocol Implementation
============================================================
Implémente le protocole de Schnorr pour prouver la connaissance d'un secret
(mot de passe) sans jamais le révéler.

Principe :
  - Le prouveur (Prover) connaît x (secret)
  - Le vérificateur (Verifier) connaît Y = g^x mod p (engagement public)
  - Protocole en 3 étapes : Commitment → Challenge → Response
"""

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Tuple


# ─── Paramètres du groupe cyclique (safe prime 2048-bit RFC 3526 Group 14) ───
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
G = 2  # Générateur standard


@dataclass
class ZKPCommitment:
    """Engagement du prouveur (étape 1)"""
    commitment_value: int   # Y_r = g^r mod p
    random_value: int       # r (secret, gardé par le prouveur)


@dataclass
class ZKPProof:
    """Preuve finale envoyée au vérificateur"""
    commitment: int         # Y_r = g^r mod p
    challenge: int          # c (généré par le vérificateur ou Fiat-Shamir)
    response: int           # s = r - c*x mod (p-1)


@dataclass
class ZKPPublicKey:
    """Clé publique derivée du secret"""
    public_value: int       # Y = g^x mod p
    p: int
    g: int


class SchnorrZKP:
    """
    Implémentation du protocole de Schnorr (Zero-Knowledge Proof).
    Permet de prouver la connaissance d'un mot de passe sans le révéler.
    """

    def __init__(self, p: int = P, g: int = G):
        self.p = p
        self.g = g
        self.q = (p - 1) // 2  # ordre du sous-groupe

    def derive_secret(self, password: str, salt: bytes) -> int:
        """
        Dérive un entier secret x depuis un mot de passe via PBKDF2.
        x est dans [1, q-1]
        """
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations=310_000,
            dklen=32,
        )
        x = int.from_bytes(dk, "big") % (self.q - 1) + 1
        return x

    def generate_public_key(self, password: str, salt: bytes) -> Tuple[ZKPPublicKey, bytes]:
        """
        Génère la clé publique Y = g^x mod p depuis le mot de passe.
        Retourne (ZKPPublicKey, salt).
        """
        if not salt:
            salt = os.urandom(32)
        x = self.derive_secret(password, salt)
        Y = pow(self.g, x, self.p)
        return ZKPPublicKey(public_value=Y, p=self.p, g=self.g), salt

    def create_commitment(self) -> ZKPCommitment:
        """
        Étape 1 : Le prouveur génère un engagement aléatoire.
        r ← random dans [1, q-1]
        Y_r = g^r mod p
        """
        r = secrets.randbelow(self.q - 1) + 1
        Y_r = pow(self.g, r, self.p)
        return ZKPCommitment(commitment_value=Y_r, random_value=r)

    def generate_challenge(self, public_key_value: int, commitment_value: int, context: str = "") -> int:
        """
        Étape 2 : Le vérificateur génère un challenge (ou Fiat-Shamir heuristique).
        c = H(Y || Y_r || context) mod q
        """
        h = hashlib.sha256()
        h.update(public_key_value.to_bytes((public_key_value.bit_length() + 7) // 8, "big"))
        h.update(commitment_value.to_bytes((commitment_value.bit_length() + 7) // 8, "big"))
        h.update(context.encode("utf-8"))
        c = int(h.hexdigest(), 16) % self.q
        return c

    def create_proof(self, password: str, salt: bytes, commitment: ZKPCommitment, challenge: int) -> ZKPProof:
        """
        Étape 3 : Le prouveur calcule la réponse.
        s = (r - c * x) mod q
        """
        x = self.derive_secret(password, salt)
        s = (commitment.random_value - challenge * x) % self.q
        return ZKPProof(
            commitment=commitment.commitment_value,
            challenge=challenge,
            response=s,
        )

    def verify_proof(self, proof: ZKPProof, public_key: ZKPPublicKey, context: str = "") -> bool:
        """
        Vérification : g^s * Y^c ≡ Y_r (mod p)
        Et que le challenge correspond bien.
        """
        # Recalcul du challenge pour anti-rejeu
        expected_challenge = self.generate_challenge(
            public_key.public_value, proof.commitment, context
        )
        if not hmac.compare_digest(
            expected_challenge.to_bytes(32, "big"),
            proof.challenge.to_bytes(32, "big"),
        ):
            return False

        # Vérification algébrique
        lhs = pow(self.g, proof.response, self.p) * pow(public_key.public_value, proof.challenge, self.p)
        lhs %= self.p
        return lhs == proof.commitment

    def interactive_verify(self, commitment_value: int, public_key_value: int, response: int, challenge: int) -> bool:
        """
        Vérification interactive (pour l'API REST).
        """
        lhs = (pow(self.g, response, self.p) * pow(public_key_value, challenge, self.p)) % self.p
        return lhs == commitment_value


# ─── Instance globale ─────────────────────────────────────────────────────────
zkp = SchnorrZKP()