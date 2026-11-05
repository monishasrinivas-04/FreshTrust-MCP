"""Prepare compact, provenance-preserving, redacted context for the LLM."""
from pathlib import PurePosixPath

from security.redaction import redact

SENSITIVE_PARTS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".cache",
    "secrets", "credentials",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}


def _sensitive_path(path):
    p = PurePosixPath(str(path).replace("\\", "/"))
    parts = {x.lower() for x in p.parts}
    name = p.name.lower()
    return (
        bool(parts.intersection(SENSITIVE_PARTS))
        or name.startswith(".env")
        or name in {"id_rsa", "id_ed25519", "credentials", "secrets", "token"}
        or p.suffix.lower() in SENSITIVE_SUFFIXES
    )


def refine_context(chunks, max_chars=9000):
    """Keep ranked evidence in order while enforcing a strict context budget.

    Each included block retains its exact source marker. If the next block does
    not fit, it is skipped rather than cutting off its source marker or content.
    """
    seen = set()
    parts = []
    used = 0

    for chunk in chunks or []:
        path = str(chunk.get("path", "")).strip()
        if not path or _sensitive_path(path):
            continue

        start_line = chunk.get("start_line")
        end_line = chunk.get("end_line")
        key = (path, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)

        safe_text = redact(str(chunk.get("text", ""))).strip()
        if not safe_text:
            continue

        block = (
            f"[SOURCE: {path}:{start_line if start_line is not None else '?'}-"
            f"{end_line if end_line is not None else '?'}]\n{safe_text}"
        )

        separator = "\n\n" if parts else ""
        remaining = max_chars - used - len(separator)
        if remaining <= 0:
            break

        if len(block) > remaining:
            # Preserve the provenance header and include a clearly marked
            # excerpt only when enough room remains for both.
            header, _, body = block.partition("\n")
            room = remaining - len(header) - len("\n[excerpt truncated]")
            if room < 300:
                break
            block = header + "\n" + body[:room].rstrip() + "\n[excerpt truncated]"

        parts.append(block)
        used += len(separator) + len(block)

        if used >= max_chars:
            break

    return "\n\n".join(parts)
