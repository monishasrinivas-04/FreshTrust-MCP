"""Secret detection and redaction for repository text, prompts and logs."""
import re

_PATTERNS = [
    (r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
     "[REDACTED_PRIVATE_KEY]"),
    (r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']+", r"\1[REDACTED]"),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "[REDACTED_GITHUB_TOKEN]"),
    (r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "[REDACTED_GITHUB_TOKEN]"),
    (r"\bsk-[A-Za-z0-9_-]{20,}\b", "[REDACTED_API_KEY]"),
    (r"(?i)(cloudflare[_-]?api[_-]?token|api[_-]?key|access[_-]?token|client[_-]?secret|password|secret)\s*([:=])\s*(['\"]?)[^'\"\s,;]+",
     r"\1\2[REDACTED]"),
    (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED_AWS_ACCESS_KEY]"),
]


def redact(value):
    text = value if isinstance(value, str) else str(value)
    for pattern, replacement in _PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.DOTALL)
    return text


def contains_sensitive_material(value):
    original = value if isinstance(value, str) else str(value)
    return redact(original) != original
