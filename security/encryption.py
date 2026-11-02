"""Optional AES-GCM helper for credentials that truly must be persisted.
Default design does not persist user PATs. Key must come from an external secret store.
"""
import os, base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def encrypt_secret(plaintext: str, key: bytes) -> str:
    if len(key) not in (16, 24, 32):
        raise ValueError("AES-GCM key must be 128, 192 or 256 bits.")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()

def decrypt_secret(encoded: str, key: bytes) -> str:
    raw = base64.urlsafe_b64decode(encoded.encode())
    return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode()
