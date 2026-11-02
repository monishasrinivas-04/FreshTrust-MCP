
import json
import re


def _get_value(obj, key, default=None):
    """Read a field from either an object or a dictionary."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def extract_mcp_text(result):
    """
    Extract useful text from MCP TextContent and EmbeddedResource blocks.

    Prefer embedded resource content over status-only TextContent messages.
    """
    resource_parts = []
    text_parts = []

    for block in _get_value(result, "content", []) or []:
        # Ordinary text response
        block_text = _get_value(block, "text")
        if isinstance(block_text, str) and block_text.strip():
            text_parts.append(block_text.strip())

        # Embedded MCP resource
        resource = _get_value(block, "resource")
        if resource is not None:
            resource_text = _get_value(resource, "text")

            if isinstance(resource_text, str) and resource_text.strip():
                resource_parts.append(resource_text.strip())

            # Support resource objects containing nested contents
            nested_contents = _get_value(resource, "contents", []) or []

            for item in nested_contents:
                nested_text = _get_value(item, "text")
                if isinstance(nested_text, str) and nested_text.strip():
                    resource_parts.append(nested_text.strip())

    # Prefer actual file/resource content when available.
    if resource_parts:
        return "\n".join(resource_parts).strip()

    structured = (
        _get_value(result, "structuredContent")
        or _get_value(result, "structured_content")
    )

    if structured is not None:
        if isinstance(structured, str):
            return structured.strip()

        return json.dumps(
            structured,
            ensure_ascii=False,
            default=str
        )

    return "\n".join(text_parts).strip()


def parse_json(text):
    """Parse JSON directly or from a Markdown JSON code block."""
    if not isinstance(text, str):
        return None

    candidates = [
        text,
        *re.findall(
            r"```(?:json)?\s*(.*?)```",
            text,
            re.S | re.I
        )
    ]

    for candidate in candidates:
        try:
            return json.loads(candidate.strip())
        except (json.JSONDecodeError, TypeError):
            continue

    return None
