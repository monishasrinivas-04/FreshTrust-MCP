"""AES-256-GCM helpers. Supply and manage the key outside this module."""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_key():
    """Generate a 256-bit key; store it in a secret manager, not source control."""
    return AESGCM.generate_key(bit_length=256)


def encrypt_data(plaintext, key, associated_data=b"FreshTrust-MCP"):
    if isinstance(key, str):
        key = base64.urlsafe_b64decode(key.encode("ascii"))
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated_data)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_data(token, key, associated_data=b"FreshTrust-MCP"):
    if isinstance(key, str):
        key = base64.urlsafe_b64decode(key.encode("ascii"))
    packed = base64.urlsafe_b64decode(token.encode("ascii"))
    if len(packed) < 13:
        raise ValueError("Invalid encrypted payload.")
    nonce, ciphertext = packed[:12], packed[12:]
    # AESGCM raises InvalidTag when the key, nonce, ciphertext or AAD is wrong.
    return AESGCM(key).decrypt(nonce, ciphertext, associated_data).decode("utf-8")
