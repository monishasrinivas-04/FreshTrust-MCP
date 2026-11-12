"""Cloudflare Workers AI client with bounded, adaptive generation retries."""
import json
import logging
import time

import requests

from config.settings import settings
from llm.prompts import build_system_prompt, classify_task, max_tokens_for_task
from security.redaction import redact

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 500, 502, 503, 504}
REQUEST_TIMEOUT = (10, 240)  # connect timeout, read timeout


class InferenceUnavailable(RuntimeError):
    pass


def _extract_text(content):
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts).strip()

    if isinstance(content, dict):
        text = content.get("text")
        return text.strip() if isinstance(text, str) else ""

    return ""


def _post_with_one_adaptive_retry(url, headers, payload):
    """Retry once on transient server timeout with a smaller generation request."""
    attempts = []
    current_payload = dict(payload)

    for attempt in range(2):
        started = time.perf_counter()
        try:
            response = requests.post(
                url,
                headers=headers,
                json=current_payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.Timeout:
            elapsed = time.perf_counter() - started
            attempts.append(elapsed)
            if attempt == 0:
                logger.warning(
                    "Cloudflare request timed out; retrying once with a smaller prompt."
                )
                current_payload = _smaller_payload(current_payload)
                time.sleep(2)
                continue
            raise InferenceUnavailable(
                "Cloudflare inference timed out twice. Reduce repository evidence "
                "or retry later."
            ) from None
        except requests.RequestException:
            raise InferenceUnavailable(
                "Cloudflare inference endpoint is unreachable. Check network connectivity."
            ) from None

        elapsed = time.perf_counter() - started
        attempts.append(elapsed)

        if response.status_code in RETRYABLE_STATUS_CODES and attempt == 0:
            logger.warning(
                "Cloudflare returned HTTP %s; retrying once with a smaller request.",
                response.status_code,
            )
            current_payload = _smaller_payload(current_payload)
            time.sleep(2)
            continue

        return response, sum(attempts), attempt + 1

    raise InferenceUnavailable("Cloudflare inference did not return a response.")


def _build_user_message(question, repository_evidence):
    """Serialize the request and untrusted repository evidence as separate JSON fields."""
    return json.dumps(
        {
            "user_request": question,
            "repository_evidence": repository_evidence,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _smaller_payload(payload):
    """Reduce repository evidence and output budget for one bounded retry."""
    reduced = dict(payload)
    messages = [dict(message) for message in payload.get("messages", [])]

    # Keep the system prompt and user question intact. Shorten only the
    # repository evidence field in the structured user message.
    if len(messages) > 1 and isinstance(messages[1].get("content"), str):
        try:
            user_data = json.loads(messages[1]["content"])
        except (TypeError, ValueError):
            user_data = None

        if (
            isinstance(user_data, dict)
            and isinstance(user_data.get("repository_evidence"), str)
        ):
            evidence = user_data["repository_evidence"]
            user_data["repository_evidence"] = evidence[
                : max(2500, len(evidence) // 2)
            ]
            messages[1]["content"] = json.dumps(
                user_data,
                ensure_ascii=False,
                separators=(",", ":"),
            )

    reduced["messages"] = messages
    current_limit = int(reduced.get("max_completion_tokens", 2048))
    reduced["max_completion_tokens"] = max(1024, current_limit // 2)
    return reduced


def generate_answer(question, context, max_tokens=None, task_type=None):
    if not settings.cloudflare_account_id or not settings.cloudflare_api_token:
        raise InferenceUnavailable("Cloudflare Workers AI is not configured.")

    task_type = task_type or classify_task(question)
    output_limit = int(max_tokens or max_tokens_for_task(task_type))
    output_limit = max(1024, min(output_limit, 4096))

    safe_question = redact(question)
    safe_context = redact(context or "")

    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.cloudflare_account_id}/ai/v1/chat/completions"
    )
    headers = {
        "Authorization": f"Bearer {settings.cloudflare_api_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": settings.cloudflare_model,
        "messages": [
            {
                "role": "system",
                "content": build_system_prompt(task_type),
            },
            {
                "role": "user",
                "content": _build_user_message(safe_question, safe_context),
            },
        ],
        "max_completion_tokens": output_limit,
        "reasoning_effort": "low",
        "temperature": 0.2,
        "stream": False,
    }

    response, elapsed, attempts = _post_with_one_adaptive_retry(
        url, headers, payload
    )

    if response.status_code in (401, 402, 403):
        raise InferenceUnavailable(
            "Cloudflare inference is unauthorized or unavailable. "
            "Check token permissions and account configuration."
        )
    if response.status_code == 429:
        raise InferenceUnavailable(
            "Cloudflare rate limit or quota reached. Retry after the limit resets."
        )
    if not response.ok:
        logger.error("Cloudflare returned HTTP %s after %s attempt(s).",
                     response.status_code, attempts)
        raise InferenceUnavailable(
            f"Cloudflare inference returned HTTP {response.status_code} "
            f"after {attempts} attempt(s)."
        )

    try:
        data = response.json()
    except ValueError:
        raise InferenceUnavailable("Cloudflare returned invalid JSON.") from None

    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise InferenceUnavailable("Cloudflare returned no completion choices.")

    choice = choices[0]
    message = choice.get("message") or {}
    answer = _extract_text(message.get("content")) if isinstance(message, dict) else ""
    finish_reason = choice.get("finish_reason")
    usage = data.get("usage") or {}

    logger.info(
        "Cloudflare generation: task=%s, finish_reason=%s, "
        "completion_tokens=%s, configured_output_limit=%s, attempts=%s, "
        "elapsed=%.2fs, evidence_chars=%s",
        task_type,
        finish_reason,
        usage.get("completion_tokens"),
        output_limit,
        attempts,
        elapsed,
        len(safe_context),
    )

    if not answer:
        if finish_reason == "length":
            raise InferenceUnavailable(
                "Qwen reached its completion-token limit without returning final text. "
                "The request may require a smaller task or shorter evidence."
            )
        raise InferenceUnavailable(
            "Cloudflare returned an empty answer. Check the model response and finish reason."
        )

    # Apply the existing secret redaction to model output as a final
    # safeguard. This is defense in depth, not a substitute for secret isolation.
    safe_answer = redact(answer.strip())
    return safe_answer, elapsed
