import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Flask
    SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-production-super-secret")
    DEBUG = os.getenv("DEBUG", "False") == "True"

    # Database
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "postgresql://zkpuser:zkppass@localhost:5432/zkp_credentials"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # JWT
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "jwt-super-secret-key-change-me")
    JWT_ACCESS_TOKEN_EXPIRES = 3600  # 1 hour

    # ZKP Parameters (Schnorr Protocol - using a safe prime)
    # p = large safe prime, g = generator
    ZKP_PRIME = int(os.getenv("ZKP_PRIME", str(
        0xFFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1
        + 0x29024E088A67CC74020BBEA63B139B22514A08798E3404DD
        + 0xEF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245
        + 0xE485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED
        + 0xEE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D
        + 0xC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F
        + 0x83655D23DCA3AD961C62F356208552BB9ED529077096966D
        + 0x670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B
        + 0xE39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9
        + 0xDE2BCBF6955817183995497CEA956AE515D2261898FA0510
        + 0x15728E5A8AACAA68FFFFFFFFFFFFFFFF
    )))
    ZKP_GENERATOR = int(os.getenv("ZKP_GENERATOR", "2"))

    # Redis (for session/nonce storage)
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Encryption
    MASTER_KEY = os.getenv("MASTER_KEY", "32-byte-master-key-for-aes256!!!")

    # Zero-Trust
    MAX_CREDENTIAL_ACCESS = int(os.getenv("MAX_CREDENTIAL_ACCESS", "5"))  # max accès par jour
    TOKEN_EXPIRY_MINUTES = int(os.getenv("TOKEN_EXPIRY_MINUTES", "30"))

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False

config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig
}
