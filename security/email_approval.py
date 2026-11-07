"""Brevo-backed, single-use approval by a preconfigured repository owner/admin."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from sqlalchemy import text
from evaluation.metrics import engine

APPROVAL_TTL_SECONDS = 600
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _question_hash(question: str) -> str:
    return hashlib.sha256(question.strip().encode("utf-8")).hexdigest()


def _ensure_table() -> None:
    # Reuse the application's configured PostgreSQL/SQLite engine so approval
    # records survive process restarts when DATABASE_URL points to PostgreSQL.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS private_access_approvals (
                request_id VARCHAR(36) PRIMARY KEY,
                token_hash VARCHAR(64) NOT NULL UNIQUE,
                requester_email VARCHAR(320) NOT NULL,
                repository VARCHAR(255) NOT NULL,
                question_hash VARCHAR(64) NOT NULL,
                pat_fingerprint VARCHAR(64) NOT NULL,
                status VARCHAR(20) NOT NULL,
                created_at BIGINT NOT NULL,
                expires_at BIGINT NOT NULL,
                decided_at BIGINT
            )
        """))


_ensure_table()


def create_pending_approval(
    approver_email: str,
    repository: str,
    question: str,
    pat_fingerprint: str,
) -> tuple[str, str]:
    email = (approver_email or "").strip()
    if not _EMAIL_RE.fullmatch(email):
        raise ValueError("The configured repository approver email is invalid.")
    if not all((repository, question.strip(), pat_fingerprint)):
        raise ValueError("Repository, question, and PAT verification are required.")

    request_id = str(uuid.uuid4())
    raw_token = secrets.token_urlsafe(32)
    now = int(time.time())
    with engine.begin() as conn:
        # Legacy column name requester_email stores the server-configured
        # repository owner/admin recipient; no recipient is accepted from UI input.
        conn.execute(text("""
            UPDATE private_access_approvals
            SET status = 'superseded', decided_at = :now
            WHERE requester_email = :email AND repository = :repo
              AND status = 'pending'
        """), {"now": now, "email": email, "repo": repository})
        conn.execute(text("""
            INSERT INTO private_access_approvals
            (request_id, token_hash, requester_email, repository, question_hash,
             pat_fingerprint, status, created_at, expires_at)
            VALUES (:id, :token_hash, :email, :repo, :question_hash,
                    :pat_fingerprint, 'pending', :now, :expires)
        """), {
            "id": request_id,
            "token_hash": _token_hash(raw_token),
            "email": email,
            "repo": repository,
            "question_hash": _question_hash(question),
            "pat_fingerprint": pat_fingerprint,
            "now": now,
            "expires": now + APPROVAL_TTL_SECONDS,
        })
    return request_id, raw_token


def get_repository_approver_email(repository: str) -> str:
    """Resolve the trusted approver from server-side configuration, never UI input.

    REPOSITORY_APPROVERS must be a JSON object such as:
    {"owner/repository": "repo-admin@example.com"}
    """
    raw_mapping = os.getenv("REPOSITORY_APPROVERS", "").strip()
    if not raw_mapping:
        raise RuntimeError("REPOSITORY_APPROVERS is not configured on the server.")
    try:
        mapping = json.loads(raw_mapping)
    except json.JSONDecodeError:
        raise RuntimeError("REPOSITORY_APPROVERS must contain a valid JSON object.") from None
    if not isinstance(mapping, dict):
        raise RuntimeError("REPOSITORY_APPROVERS must be a JSON object mapping repositories to emails.")

    approver = next(
        (value for key, value in mapping.items()
         if isinstance(key, str) and key.strip().casefold() == repository.strip().casefold()),
        None,
    )
    if not isinstance(approver, str) or not _EMAIL_RE.fullmatch(approver.strip()):
        raise RuntimeError(f"No valid owner/admin approver is configured for {repository}.")
    return approver.strip()


def send_approval_email(approver_email: str, repository: str, question: str, raw_token: str) -> None:
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    from_email = os.getenv("BREVO_FROM_EMAIL", "").strip()
    from_name = os.getenv("BREVO_FROM_NAME", "FreshTrust-MCP").strip() or "FreshTrust-MCP"
    base_url = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    if not api_key or not from_email:
        raise RuntimeError("Configure BREVO_API_KEY and BREVO_FROM_EMAIL before requesting email approval.")
    if not base_url:
        raise RuntimeError("Configure APP_BASE_URL with the public HTTPS URL of the deployed application.")
    parsed_base = urlsplit(base_url)
    local_http = parsed_base.hostname in {"localhost", "127.0.0.1"} and parsed_base.scheme == "http"
    if not parsed_base.netloc or (parsed_base.scheme != "https" and not local_http):
        raise RuntimeError("APP_BASE_URL must use HTTPS (HTTP is allowed only for localhost development).")
    if not _EMAIL_RE.fullmatch(from_email):
        raise RuntimeError("BREVO_FROM_EMAIL is not a valid sender email address.")

    approval_url = f"{base_url}/email-approval/{raw_token}"
    safe_repo = html.escape(repository)
    safe_question = html.escape(question.strip())
    safe_url = html.escape(approval_url, quote=True)
    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:640px;margin:auto;line-height:1.5">
      <h2>FreshTrust-MCP private repository access request</h2>
      <p>A private repository analysis request requires approval from its preconfigured owner/admin.</p>
      <p><strong>Repository:</strong> {safe_repo}</p>
      <p><strong>Question:</strong> {safe_question}</p>
      <p>Approving allows FreshTrust-MCP to perform read-only repository search,
      retrieve relevant files and commit metadata, and generate an answer.</p>
      <p>This approval link expires in 10 minutes and can be used only once.
      The requester's GitHub PAT is not included in this email.</p>
      <p><a href="{safe_url}">Review approval request</a></p>
      <p>If you are not an authorized owner/admin for this repository, reject the request.</p>
    </div>
    """
    plain_text = (
        "FreshTrust-MCP private repository access request\n"
        f"Repository: {repository}\nQuestion: {question.strip()}\n\n"
        "An authorized repository owner/admin must approve this read-only analysis request.\n"
        "The link expires in 10 minutes and can be used once.\n"
        f"Review: {approval_url}\n"
    )
    payload = json.dumps({
        "sender": {"name": from_name, "email": from_email},
        "to": [{"email": approver_email}],
        "subject": f"Approve private repository analysis: {repository}",
        "htmlContent": body,
        "textContent": plain_text,
    }).encode("utf-8")
    request = Request(
        "https://api.brevo.com/v3/smtp/email",
        data=payload,
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            if response.status not in (200, 201, 202):
                raise RuntimeError(f"Brevo rejected the email request (HTTP {response.status}).")
    except HTTPError as exc:
        # Surface only the provider's short error message, never request headers or secrets.
        try:
            details = json.loads(exc.read(2048).decode("utf-8", errors="replace"))
            provider_message = str(details.get("message") or details.get("code") or "")[:240]
        except Exception:
            provider_message = ""
        suffix = f": {provider_message}" if provider_message else ""
        raise RuntimeError(f"Brevo could not send the approval email (HTTP {exc.code}){suffix}.") from None
    except (URLError, TimeoutError):
        raise RuntimeError("Could not connect to Brevo to send the approval email.") from None


def get_approval_page(token: str) -> dict | None:
    now = int(time.time())
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT repository, question_hash, status, expires_at
            FROM private_access_approvals WHERE token_hash = :token_hash
        """), {"token_hash": _token_hash(token)}).mappings().first()
    if not row:
        return None
    if row["status"] != "pending" or int(row["expires_at"]) <= now:
        return {"status": "expired" if int(row["expires_at"]) <= now else row["status"]}
    return {"status": "pending", "repository": row["repository"]}


def decide_approval(token: str, decision: str) -> bool:
    if decision not in {"approved", "rejected"}:
        return False
    now = int(time.time())
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE private_access_approvals
            SET status = :decision, decided_at = :now
            WHERE token_hash = :token_hash AND status = 'pending' AND expires_at > :now
        """), {
            "decision": decision,
            "now": now,
            "token_hash": _token_hash(token),
        })
    return result.rowcount == 1


def consume_approved_request(
    request_id: str,
    repository: str,
    question: str,
    pat_fingerprint: str,
) -> bool:
    """Atomically consume a matching, approved request before private retrieval."""
    now = int(time.time())
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE private_access_approvals
            SET status = 'consumed', decided_at = :now
            WHERE request_id = :id AND repository = :repo
              AND question_hash = :question_hash
              AND pat_fingerprint = :pat_fingerprint
              AND status = 'approved' AND expires_at > :now
        """), {
            "now": now,
            "id": request_id,
            "repo": repository,
            "question_hash": _question_hash(question),
            "pat_fingerprint": pat_fingerprint,
        })
    return result.rowcount == 1


def mark_email_failed(request_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE private_access_approvals SET status = 'email_failed'
            WHERE request_id = :id AND status = 'pending'
        """), {"id": request_id})
