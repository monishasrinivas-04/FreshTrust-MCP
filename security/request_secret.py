"""Request-scoped AES-GCM handling for private GitHub credentials."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_secret_context = ContextVar("freshtrust_encrypted_pat", default=None)

@contextmanager
def encrypted_request_secret(token: str):
    token = (token or "").strip()
    if not token:
        marker = _secret_context.set(None)
        try:
            yield
        finally:
            _secret_context.reset(marker)
        return

    key = bytearray(AESGCM.generate_key(bit_length=256))
    nonce = os.urandom(12)
    aad = b"FreshTrust-MCP/request-token/v1"
    ciphertext = AESGCM(bytes(key)).encrypt(nonce, token.encode("utf-8"), aad)
    marker = _secret_context.set((bytes(key), nonce, ciphertext, aad))
    try:
        yield
    finally:
        _secret_context.reset(marker)
        for i in range(len(key)):
            key[i] = 0

def get_request_secret() -> str:
    envelope = _secret_context.get()
    if envelope is None:
        return ""
    key, nonce, ciphertext, aad = envelope
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad).decode("utf-8")
    except Exception as exc:
        raise PermissionError("The request credential could not be authenticated.") from exc
