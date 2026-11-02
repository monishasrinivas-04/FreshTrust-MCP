import logging, re

class SecretRedactionFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[REDACTED]", message)
        message = re.sub(r"(?i)(gh[pousr]_[A-Za-z0-9_]{20,})", "[REDACTED_TOKEN]", message)
        record.msg, record.args = message, ()
        return True

def configure_logging():
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
