"""Configure process-wide secret redaction for Python logging."""
import logging
from security.redaction import redact


class SecretRedactionFilter(logging.Filter):
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            message = "[UNREADABLE LOG MESSAGE]"
        record.msg = redact(message)
        record.args = ()
        return True


def configure_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(SecretRedactionFilter())
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(SecretRedactionFilter())
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
        root.addHandler(handler)
