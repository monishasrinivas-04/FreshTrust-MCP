
"""Administrator GitHub OAuth connection using the official MCP SDK.

Public repository OAuth only. Private repositories must use request-scoped PATs.
OAuth tokens and client information are stored through NeonOAuthTokenStorage.
"""

from __future__ import annotations

import asyncio
import logging
import json
import queue
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientMetadata

from config.settings import settings
from security.oauth_storage import NeonOAuthTokenStorage

logger = logging.getLogger(__name__)

REQUESTED_SCOPE = "public_repo"
OAUTH_TIMEOUT_SECONDS = 300


class PublicRepoOAuthClientProvider(OAuthClientProvider):
    """MCP OAuth provider that requests only public_repo."""

    async def _perform_authorization(self) -> Any:
        self.context.client_metadata.scope = REQUESTED_SCOPE
        return await super()._perform_authorization()

    async def _parse_github_token_response(self, response: Any) -> Any:
        """Normalize GitHub's form-encoded OAuth response for the MCP SDK.

        GitHub's OAuth token endpoint may return
        application/x-www-form-urlencoded while the MCP SDK helper expects
        a JSON OAuthToken response. Normalize the body without logging it.
        """
        import httpx
        from mcp.client.auth.oauth2 import handle_token_response_scopes

        raw_body = response.text
        content_type = response.headers.get("content-type", "").lower()

        try:
            if "json" in content_type or raw_body.lstrip().startswith("{"):
                payload = json.loads(raw_body)
            else:
                parsed = parse_qs(raw_body, keep_blank_values=True)
                payload = {
                    key: values[-1] if values else ""
                    for key, values in parsed.items()
                }
        except Exception:
            raise RuntimeError(
                "GitHub returned an unreadable OAuth token response."
            ) from None

        if not isinstance(payload, dict):
            raise RuntimeError("GitHub returned an invalid OAuth token response.")

        if payload.get("error"):
            raise RuntimeError("GitHub rejected the OAuth token exchange.")

        if not payload.get("access_token"):
            raise RuntimeError(
                "GitHub's OAuth response did not contain an access token."
            )

        # Re-wrap the normalized payload so the SDK's parser and scope
        # handling remain in use.
        normalized_response = httpx.Response(
            status_code=response.status_code,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode("utf-8"),
        )
        return await handle_token_response_scopes(normalized_response)

    async def _store_verified_token_response(self, response: Any) -> None:
        token_response = await self._parse_github_token_response(response)
        granted = (token_response.scope or "").replace(",", " ").split()

        if set(granted) != {REQUESTED_SCOPE}:
            raise RuntimeError(
                "GitHub did not grant exactly the public_repo scope. "
                "The OAuth token was not saved."
            )

        self.context.current_tokens = token_response
        self.context.update_token_expiry(token_response)
        await self.context.storage.set_tokens(token_response)

    async def _handle_token_response(self, response: Any) -> None:
        await self._store_verified_token_response(response)

    async def _handle_refresh_response(self, response: Any) -> None:
        # Revalidate scope on refresh; never persist broader permissions.
        await self._store_verified_token_response(response)

@dataclass
class OAuthTransaction:
    authorization_url: str | None = None
    callback_queue: queue.Queue = field(default_factory=queue.Queue)
    status: str = "starting"
    message: str = "Starting GitHub authorization."
    started: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_transaction: OAuthTransaction | None = None
_transaction_lock = threading.Lock()


def _redirect_uri() -> str:
    value = settings.github_oauth_redirect_uri.strip()
    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("Invalid GitHub OAuth redirect URI.")

    if not parsed.path or parsed.path == "/":
        raise RuntimeError("The GitHub OAuth redirect URI must include a callback path.")

    return value


async def _run_oauth_flow(transaction: OAuthTransaction) -> None:
    storage = NeonOAuthTokenStorage()
    redirect_uri = _redirect_uri()

    client_metadata = OAuthClientMetadata(
        redirect_uris=[redirect_uri],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=REQUESTED_SCOPE,
    )

    async def redirect_handler(url: str) -> None:
        with transaction.lock:
            transaction.authorization_url = url
            transaction.status = "awaiting_authorization"
            transaction.message = "Open the GitHub authorization link to continue."

    async def callback_handler() -> tuple[str, str | None]:
        try:
            code, state, error = await asyncio.to_thread(
                transaction.callback_queue.get,
                True,
                OAUTH_TIMEOUT_SECONDS,
            )
        except queue.Empty as exc:
            raise TimeoutError("GitHub OAuth callback timed out.") from exc

        if error:
            raise RuntimeError("GitHub authorization was cancelled or rejected.")

        return code, state

    provider = PublicRepoOAuthClientProvider(
        server_url=settings.github_mcp_url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=OAUTH_TIMEOUT_SECONDS,
    )

    async with streamablehttp_client(
        settings.github_mcp_url,
        auth=provider,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

    tokens = await storage.get_tokens()
    if tokens is None or not tokens.access_token:
        raise RuntimeError("GitHub did not provide an access token.")

    # GitHub normally returns the granted scopes in the token response.
    # Do not claim that the permission boundary is verified if scope is absent.
    granted_scope = (tokens.scope or "").replace(",", " ").split()
    if not granted_scope:
        with transaction.lock:
            transaction.status = "scope_unverified"
            transaction.message = (
                "OAuth completed, but GitHub did not report granted scopes. "
                "Public-only permissions have not been verified."
            )
        return

    if set(granted_scope) != {REQUESTED_SCOPE}:
        with transaction.lock:
            transaction.status = "scope_mismatch"
            transaction.message = (
                "GitHub returned scopes different from public_repo. "
                "Do not use this connection until its permissions are reviewed."
            )
        return

    with transaction.lock:
        transaction.status = "connected"
        transaction.message = "GitHub public-repository authorization completed."


def _oauth_worker(transaction: OAuthTransaction) -> None:
    try:
        asyncio.run(_run_oauth_flow(transaction))
        with transaction.lock:
            if transaction.status == "starting":
                transaction.status = "connected"
                transaction.message = "GitHub authorization completed."
    except Exception as exc:
        # Do not log callback URLs, authorization codes, tokens, or exception text.
        logger.error("GitHub OAuth flow failed (%s).", type(exc).__name__)
        with transaction.lock:
            transaction.status = "failed"
            transaction.message = (
                "GitHub authorization failed. Check server configuration "
                "and application logs."
            )


def start_admin_oauth() -> dict[str, str]:
    """Start the administrator OAuth flow in a background thread."""
    global _transaction

    with _transaction_lock:
        if _transaction is not None and _transaction.status in {
            "starting",
            "awaiting_authorization",
        }:
            raise RuntimeError("A GitHub OAuth connection is already in progress.")

        transaction = OAuthTransaction()
        _transaction = transaction

        worker = threading.Thread(
            target=_oauth_worker,
            args=(transaction,),
            name="freshtrust-github-oauth",
            daemon=True,
        )
        worker.start()

    return get_admin_oauth_status()


def get_admin_oauth_status() -> dict[str, str]:
    """Return safe OAuth status information for the application UI."""
    with _transaction_lock:
        transaction = _transaction

    if transaction is None:
        return {
            "status": "not_started",
            "message": "GitHub administrator authorization has not been started.",
            "authorization_url": "",
        }

    with transaction.lock:
        return {
            "status": transaction.status,
            "message": transaction.message,
            "authorization_url": transaction.authorization_url or "",
        }


def complete_admin_oauth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
) -> bool:
    """Pass the GitHub callback to the MCP SDK callback handler.

    The MCP SDK performs the authoritative OAuth state validation.
    """
    with _transaction_lock:
        transaction = _transaction

    if transaction is None:
        return False

    with transaction.lock:
        if transaction.status != "awaiting_authorization":
            return False

    if error:
        transaction.callback_queue.put(("", state, error))
        return True

    if not code or not state:
        return False

    transaction.callback_queue.put((code, state, ""))
    return True
