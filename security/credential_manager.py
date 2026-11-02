"""Request-scoped credentials. Tokens are never persisted by this module."""
from contextlib import contextmanager

@contextmanager
def request_credential(token: str):
    value = (token or "").strip()
    try:
        yield value or None
    finally:
        # Python strings cannot be reliably zeroized; scope is kept short and no copy is stored.
        value = None
