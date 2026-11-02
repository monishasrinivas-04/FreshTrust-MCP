import re
def redact(value):
    text = str(value)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b", "[REDACTED_TOKEN]", text)
    return text
