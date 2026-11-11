"""GitHub access for FreshTrust-MCP.

Public repositories use anonymous GitHub REST/raw HTTPS access.
Private repositories use the official GitHub MCP Server with a request-scoped PAT.
Private credentials are not persisted or logged.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import threading
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from config.settings import settings
from mcp_layer.response_parser import extract_mcp_text
from security.authorization import (
    authorize_tool,
    validate_repository,
    validate_repository_path,
)

log = logging.getLogger(__name__)

DEFAULT_CANDIDATE_PATHS = (
    "README.md", "pyproject.toml", "requirements.txt", "package.json",
    "main.py", "app.py", "src/main.py",
)
SUPPORTED_SUFFIXES = {
    ".py", ".md", ".txt", ".rst", ".toml", ".yaml", ".yml", ".json",
    ".js", ".jsx", ".ts", ".tsx", ".sql", ".sh", ".ipynb", ".java",
    ".c", ".cpp", ".h", ".html", ".css",
}
IGNORED_PARTS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist",
    "build", ".next", ".idea",
}
SCOPE_INPUT_PATTERN = re.compile(r"(?<!\S)(?:repo|org|user):", re.IGNORECASE)


class GitHubMCPAuthenticationRequired(RuntimeError):
    """Raised when the public MCP process needs its first OAuth authorization."""
    def __init__(self, message: str, authorization_url: str = ""):
        super().__init__(message)
        self.authorization_url = authorization_url


def _normalise_visibility(visibility: Optional[str]) -> str:
    value = (visibility or "").strip().lower()
    if value not in {"public", "private"}:
        raise ValueError("Visibility must be Public or Private.")
    return value


def _server_parameters(token: Optional[str] = None, public_oauth: bool = False):
    """
    Build stdio parameters for the official GitHub MCP Server.

    On Windows/local development, Docker Desktop launches the official MCP
    container. On the Render Docker image, the bundled native executable is
    launched directly. A private PAT is passed only to the request-scoped
    child process and is never written to disk or logged.

    The legacy public_oauth branch is retained for compatibility with
    _PersistentPublicMCP. The active public-repository retrieval path remains
    the anonymous GitHub REST/raw HTTPS implementation.
    """
    env = os.environ.copy()
    env.pop("GITHUB_PERSONAL_ACCESS_TOKEN", None)

    if public_oauth:
        port = settings.github_mcp_oauth_callback_port
        env["GITHUB_OAUTH_CALLBACK_PORT"] = str(port)
        args = [
            "run", "-i", "--rm",
            "-p", f"127.0.0.1:{port}:{port}",
            "-e", f"GITHUB_OAUTH_CALLBACK_PORT={port}",
            "-e", f"GITHUB_TOOLSETS={settings.github_mcp_toolsets}",
            settings.github_mcp_image, "stdio", "--read-only",
        ]
        return StdioServerParameters(
            command=settings.github_mcp_docker_command,
            args=args,
            env=env,
        )

    env["GITHUB_TOOLSETS"] = settings.github_mcp_toolsets
    if token and token.strip():
        env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token.strip()

    # Windows local development: use Docker Desktop because the Render
    # executable path (/usr/local/bin/...) is not available on Windows.
    use_docker = os.name == "nt" or os.getenv("GITHUB_MCP_USE_DOCKER", "").lower() in {
        "1", "true", "yes"
    }

    if use_docker:
        args = [
            "run", "-i", "--rm",
            "-e", "GITHUB_TOOLSETS",
            "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            settings.github_mcp_image,
            "stdio", "--read-only",
        ]
        return StdioServerParameters(
            command=settings.github_mcp_docker_command,
            args=args,
            env=env,
        )

    # Render/Linux deployment: use the native executable built into the image.
    binary_path = os.getenv(
        "GITHUB_MCP_BINARY_PATH",
        "/usr/local/bin/github-mcp-server",
    )
    return StdioServerParameters(
        command=binary_path,
        args=["stdio", "--read-only"],
        env=env,
    )



def _tool_schema(tool: Any) -> Dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    return schema if isinstance(schema, dict) else {}


def _find_tool(tools: List[Any], name: str) -> Any:
    matches = [tool for tool in tools if getattr(tool, "name", None) == name]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one GitHub MCP tool named {name!r}.")
    return matches[0]


def _text(result: Any) -> str:
    try:
        return extract_mcp_text(result) or ""
    except Exception:
        blocks = getattr(result, "content", None) or []
        return "\n".join(
            str(getattr(block, "text", ""))
            for block in blocks if getattr(block, "text", None)
        )


def _raise_if_auth_challenge(value: Any) -> None:
    text = value if isinstance(value, str) else str(value)
    lowered = text.lower()
    auth_words = (
        "authorize", "authorization", "device code", "sign in to github",
        "authenticate", "oauth", "login to github",
    )
    if not any(word in lowered for word in auth_words):
        return
    urls = re.findall(r"https://(?:github\.com|githubcopilot\.com)/[^\s<>\]\)\"']+", text)
    url = next((u.rstrip(".,;:") for u in urls if "login" in u.lower() or "authorize" in u.lower() or "device" in u.lower()), "")
    if not url:
        url = next((u.rstrip(".,;:") for u in urls), "")
    message = (
        "GitHub authorization is required for the public MCP connection. "
        "Open the authorization link shown below and approve access. The browser "
        "must be able to reach the Docker-published callback at localhost:8085. "
        "After approval, return to FreshTrust and submit the question again. "
        "The public MCP connection stays alive while this FreshTrust process runs."
    )
    if url:
        message += f"\n\n[Open GitHub authorization]({url})"
    else:
        # Keep the server's instructions visible, but do not mistake them for files.
        safe_text = text[:1200]
        message += f"\n\nGitHub MCP response:\n{safe_text}"
    raise GitHubMCPAuthenticationRequired(message, url)


def _decode_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    text = _text(result)
    _raise_if_auth_challenge(text)
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        match = re.search(r"\{.*\}|\[.*\]", text, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return text


def _walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _repo_items(payload: Any) -> List[Dict[str, Any]]:
    candidates = []
    if isinstance(payload, dict):
        for key in ("items", "repositories", "repos", "results"):
            if isinstance(payload.get(key), list):
                candidates.extend(payload[key])
        candidates.append(payload)
    elif isinstance(payload, list):
        candidates.extend(payload)
    candidates.extend(
        item for item in _walk_json(payload)
        if isinstance(item, dict) and (
            isinstance(item.get("full_name"), str)
            or (isinstance(item.get("name"), str) and isinstance(item.get("owner"), dict))
        )
    )
    output, seen = [], set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner")
        owner_login = owner.get("login") if isinstance(owner, dict) else None
        full_name = item.get("full_name") or (
            f"{owner_login}/{item['name']}" if owner_login and item.get("name") else ""
        )
        if not isinstance(full_name, str) or "/" not in full_name:
            continue
        # Search is explicitly is:private; do not silently include public repos.
        if item.get("private") is False:
            continue
        full_name = full_name.strip()
        if full_name.lower() not in seen:
            seen.add(full_name.lower())
            output.append({
                "full_name": full_name,
                "private": bool(item.get("private", True)),
                "html_url": item.get("html_url") or f"https://github.com/{full_name}",
                "description": item.get("description") or "",
            })
    return output


def _build_discovery_args(tool: Any) -> Dict[str, Any]:
    schema = _tool_schema(tool)
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    query_key = next((key for key in ("query", "q") if key in props), None)
    if query_key is None:
        raise RuntimeError("search_repositories has no supported query parameter.")
    args = {query_key: "is:private"}
    for key, value in (("perPage", 100), ("per_page", 100), ("page", 1), ("sort", "updated"), ("order", "desc")):
        if key in props and key not in args:
            args[key] = value
    if required - set(args):
        raise RuntimeError("The GitHub MCP repository-search tool requires unsupported arguments.")
    return args


async def _discover_repositories_async(token: str) -> List[Dict[str, Any]]:
    if not token or not token.strip():
        raise PermissionError("Enter your GitHub Personal Access Token first.")
    async with stdio_client(_server_parameters(token=token)) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = list((await session.list_tools()).tools)
            me_tool = _find_tool(tools, "get_me")
            search_tool = _find_tool(tools, "search_repositories")
            me_result = await session.call_tool(me_tool.name, arguments={})

            if getattr(me_result, "isError", False):
                    detail = _text(me_result).strip()

                    if not detail:
                        detail = "GitHub MCP returned an error without details."

                    # Prevent accidental exposure of GitHub tokens in error messages.
                    detail = re.sub(
                        r"(?i)\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+)\b",
                        "[REDACTED]",
                        detail,
                    )

                    raise PermissionError(
                        f"GitHub MCP get_me failed: {detail[:1000]}"
                    )
            result = await session.call_tool(search_tool.name, arguments=_build_discovery_args(search_tool))
            if getattr(result, "isError", False):
                _raise_if_auth_challenge(_text(result))
                raise PermissionError("GitHub could not list repositories accessible to this token.")
            repos = _repo_items(_decode_payload(result))
            return sorted(repos, key=lambda item: item["full_name"].lower())


def discover_repositories(token: str) -> List[Dict[str, Any]]:
    try:
        return asyncio.run(_discover_repositories_async(token))
    except RuntimeError as exc:
        if "asyncio.run()" in str(exc):
            raise RuntimeError("Repository discovery cannot run inside an active event loop.") from None
        raise


def _build_search_query(owner: str, repo: str, question: str) -> str:
    question = (question or "").strip()
    if SCOPE_INPUT_PATTERN.search(question):
        raise ValueError("Remove GitHub repository, organization, or user qualifiers from the question.")
    query = f"repo:{owner}/{repo}"
    if question:
        query += f" {question}"
    if len(query) > 256:
        raise ValueError("The GitHub code-search query exceeds GitHub's 256-character limit.")
    return query


def _extract_paths(text: str) -> List[str]:
    _raise_if_auth_challenge(text)
    found: List[str] = []
    def walk(value: Any):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"path", "file_path", "filename"} and isinstance(child, str):
                    found.append(child)
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    try:
        walk(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        pass
    pattern = r'(?<![\w.-])(?:[\w.-]+/)*[\w.-]+\.(?:py|md|txt|rst|toml|yaml|yml|json|js|jsx|ts|tsx|sql|sh|java|c|cpp|h|ipynb|html|css)'
    found.extend(re.findall(pattern, text or "", flags=re.IGNORECASE))
    output, seen = [], set()
    for path in found:
        path = path.strip().strip("`'\"")
        try:
            validate_repository_path(path)
        except PermissionError:
            continue
        if any(part.lower() in IGNORED_PARTS for part in path.split("/")):
            continue
        suffix = "." + path.rsplit(".", 1)[-1].lower()
        if suffix in SUPPORTED_SUFFIXES and path not in seen:
            seen.add(path)
            output.append(path)
    return output


def _clean_file_text(text: str) -> str:
    _raise_if_auth_challenge(text)
    lines = (text or "").strip().splitlines()
    markers = ("successfully downloaded text file", "successfully retrieved file")
    while lines and any(marker in lines[0].lower() for marker in markers):
        lines.pop(0)
    cleaned = "\n".join(lines).strip()
    if not cleaned:
        return ""
    # Guard against MCP auth/error pages being indexed as repository content.
    low = cleaned.lower()
    if "device authorization" in low or "authorize github" in low or "login to github" in low:
        _raise_if_auth_challenge(cleaned)
    return cleaned


class _PersistentPublicMCP:
    """Own one Docker stdio process/session on a dedicated asyncio thread."""
    def __init__(self):
        self._thread = None
        self._loop = None
        self._session = None
        self._stack = None
        self._ready = threading.Event()
        self._startup_error = None
        self._call_lock = threading.Lock()

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    async def _open(self):
        self._stack = AsyncExitStack()
        read_stream, write_stream = await self._stack.enter_async_context(
            stdio_client(_server_parameters(public_oauth=True))
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()
        await self._session.list_tools()

    def _submit(self, coroutine, timeout=None):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout or settings.github_mcp_timeout_seconds)

    def _ensure_started(self):
        if self._thread is None:
            with self._call_lock:
                if self._thread is None:
                    self._thread = threading.Thread(
                        target=self._thread_main,
                        name="freshtrust-public-github-mcp",
                        daemon=True,
                    )
                    self._thread.start()
                    self._ready.wait(timeout=10)
        if self._session is None:
            try:
                self._submit(self._open(), timeout=45)
            except Exception as exc:
                self._startup_error = exc
                self.close()
                raise RuntimeError(
                    "Could not start the persistent public GitHub MCP Server. "
                    "Check Docker is running and that loopback port "
                    f"{settings.github_mcp_oauth_callback_port} is available."
                ) from None

    async def _call(self, name: str, arguments: Dict[str, Any]):
        if self._session is None:
            raise RuntimeError("The public GitHub MCP session is not initialized.")
        return await self._session.call_tool(name, arguments=arguments)

    def call_tool(self, name: str, arguments: Dict[str, Any]):
        self._ensure_started()
        with self._call_lock:
            try:
                result = self._submit(self._call(name, arguments))
            except concurrent.futures.TimeoutError:
                raise RuntimeError("The public GitHub MCP request timed out.") from None
            except GitHubMCPAuthenticationRequired:
                raise
            except Exception as exc:
                _raise_if_auth_challenge(str(exc))
                raise RuntimeError(
                    f"The persistent GitHub MCP tool '{name}' failed ({type(exc).__name__})."
                ) from None
        if getattr(result, "isError", False):
            _raise_if_auth_challenge(_text(result))
            raise RuntimeError(f"GitHub MCP tool '{name}' returned an error.")
        _raise_if_auth_challenge(_text(result))
        return result

    def close(self):
        if self._loop is not None and self._loop.is_running():
            try:
                if self._stack is not None:
                    self._submit(self._stack.aclose(), timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._session = None
        self._stack = None
        self._thread = None
        self._loop = None
        self._ready.clear()


_PUBLIC_MCP = _PersistentPublicMCP()


def close_public_mcp():
    """Close the persistent public MCP process during application shutdown."""
    _PUBLIC_MCP.close()


async def _fetch_files_with_session(
    session: Any, owner: str, repo: str, question: str, max_files: int,
    token: Optional[str] = None,
):
    tools = list((await session.list_tools()).tools)
    file_tool = _find_tool(tools, "get_file_contents")
    search_tool = _find_tool(tools, "search_code")
    file_props = _tool_schema(file_tool).get("properties", {}) or {}
    file_required = set(_tool_schema(file_tool).get("required", []) or [])
    if not {"owner", "repo", "path"}.issubset(file_props) or not {"owner", "repo"}.issubset(file_required):
        raise RuntimeError("get_file_contents has an unexpected input schema.")
    search_schema = _tool_schema(search_tool)
    search_props = search_schema.get("properties", {}) or {}
    search_required = set(search_schema.get("required", []) or [])
    if "query" not in search_props or "query" not in search_required:
        raise RuntimeError("search_code has an unexpected input schema.")

    paths = []
    if question.strip():
        query = _build_search_query(owner, repo, question)
        args = {"query": query}
        authorize_tool("search_code", args, owner, repo)
        search_result = await session.call_tool("search_code", arguments=args)
        if getattr(search_result, "isError", False):
            _raise_if_auth_challenge(_text(search_result))
            raise RuntimeError("GitHub MCP code search failed.")
        paths.extend(_extract_paths(_text(search_result)))
    if not paths:
        paths.extend(DEFAULT_CANDIDATE_PATHS)
    paths = list(dict.fromkeys(paths))[:max_files]

    async def fetch_one(path):
        args = {"owner": owner, "repo": repo, "path": path}
        authorize_tool("get_file_contents", args, owner, repo)
        result = await session.call_tool("get_file_contents", arguments=args)
        if getattr(result, "isError", False):
            _raise_if_auth_challenge(_text(result))
            return None
        content = _clean_file_text(_text(result))
        if not content:
            return None
        return {"path": path, "content": content}

    fetched = await asyncio.gather(*(fetch_one(path) for path in paths), return_exceptions=True)
    for item in fetched:
        if isinstance(item, GitHubMCPAuthenticationRequired):
            raise item
    files = [item for item in fetched if isinstance(item, dict)]
    if not files:
        raise RuntimeError(
            f"GitHub MCP returned no readable files from {owner}/{repo}. "
            "The repository may be empty, inaccessible, or the MCP tools may not expose its files."
        )

    # File contents come from GitHub MCP. Commit metadata is obtained from the
    # GitHub API using this request's PAT; the token is never stored or logged.
    semaphore = asyncio.Semaphore(6)

    async def enrich(item):
        async with semaphore:
            return await asyncio.to_thread(
                _attach_commit_metadata, owner, repo, item, token
            )

    return await asyncio.gather(*(enrich(item) for item in files))


async def _retrieve_private_async(owner, repo, question, max_files, token):
    async with stdio_client(_server_parameters(token=token)) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await _fetch_files_with_session(
                session, owner, repo, question, max_files, token=token
            )


class GitHubMCPClient:
    """GitHub MCP client compatible with the existing LangGraph orchestrator."""
    def __init__(self, token: Optional[str] = None):
        self._token = (token or "").strip()

    @staticmethod
    def discover_repositories(token: str) -> List[Dict[str, Any]]:
        return discover_repositories(token)

    @staticmethod
    def get_repository_owner_email(token: str, repository_full_name: str) -> str:
        """Resolve an email belonging to the personal repo owner or an org owner."""
        return _get_repository_owner_email(token, repository_full_name)

    async def retrieve(
        self,
        owner: str,
        repo: str,
        question: str = "",
        max_files: int = 6,
        visibility: str = "Public",
        token: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        validate_repository(owner, repo)
        visibility_value = _normalise_visibility(visibility)
        if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
            raise ValueError("max_files must be a positive integer.")
        max_files = min(max_files, 30)

        if visibility_value == "private":
            secret = (token or self._token or "").strip()
            if not secret:
                raise PermissionError("A GitHub PAT is required for a private repository.")
            try:
                return await _retrieve_private_async(owner, repo, question or "", max_files, secret)
            except GitHubMCPAuthenticationRequired:
                raise
            except Exception as exc:

                def describe_exception(error):
                    if isinstance(error, BaseExceptionGroup):
                        return " | ".join(
                            describe_exception(child)
                            for child in error.exceptions
                        )

                    detail = " ".join(str(error).split())

                    # Redact GitHub PATs before writing diagnostic information to logs.
                    detail = re.sub(
                        r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,})\b",
                        "[REDACTED_GITHUB_TOKEN]",
                        detail,
                    )

                    detail = re.sub(
                        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
                        r"\1[REDACTED]",
                        detail,
                    )

                    return f"{type(error).__name__}: {detail[:500]}"

                error_details = describe_exception(exc)

                log.error(
                    "Private GitHub MCP retrieval failed for %s/%s: %s",
                    owner,
                    repo,
                    error_details[:2000],
                )

                raise RuntimeError(
                    "Private GitHub MCP retrieval failed. Check the application logs for the diagnostic reason."
                ) from None

        # Run the synchronous bridge away from LangGraph's asyncio event loop.
        try:
            return await asyncio.to_thread(
                _retrieve_public_sync, owner, repo, question or "", max_files
            )
        except Exception as exc:
            detail = " ".join(str(exc).split())

            # Redact token-shaped strings before logging.
            detail = re.sub(
                r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,})\b",
                "[REDACTED_GITHUB_TOKEN]",
                detail,
            )
            detail = re.sub(
                r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
                r"\1[REDACTED]",
                detail,
            )

            log.error(
                "Public GitHub retrieval failed for %s/%s (%s): %s",
                owner,
                repo,
                type(exc).__name__,
                detail[:400] or "No exception detail was provided.",
            )

            raise RuntimeError(
                "Anonymous public GitHub retrieval failed. Check the Render service logs for the diagnostic reason."
            ) from None



def _github_json(url: str, token: Optional[str] = None) -> Any:
    """Fetch GitHub API JSON; private requests use only the supplied request-scoped PAT."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "FreshTrust-MCP",
    }
    if token and token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"

    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=settings.github_mcp_timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            raise FileNotFoundError("GitHub repository/resource not found or is not accessible to this request.") from None
        if exc.code == 403:
            raise RuntimeError("GitHub API access was denied or rate-limited. Retry after the limit resets.") from None
        raise RuntimeError(f"GitHub API request failed with HTTP {exc.code}.") from None
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not connect to GitHub ({type(exc).__name__}).") from None


def _is_valid_github_email(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value.strip())
    )


def _get_verified_pat_email(token: str) -> str:
    """Return the primary verified email belonging to the PAT's authenticated user."""
    try:
        email_records = _github_json(
            "https://api.github.com/user/emails?per_page=100", token=token
        )
    except Exception:
        raise PermissionError(
            "GitHub did not allow FreshTrust to read the PAT owner's email addresses. "
            "For a classic PAT, grant the user:email scope. For a fine-grained PAT, "
            "grant the Email addresses user permission (read). Also check organization "
            "SSO authorization and GitHub API rate limits."
        ) from None

    if not isinstance(email_records, list):
        raise PermissionError("GitHub returned an unexpected email-address response.")

    verified = [
        item for item in email_records
        if isinstance(item, dict)
        and item.get("verified") is True
        and _is_valid_github_email(item.get("email"))
    ]
    if not verified:
        raise PermissionError(
            "GitHub did not return a verified email address for the authenticated PAT account. "
            "Verify an email address in GitHub and grant the PAT email-read permission."
        )

    primary = next((item for item in verified if item.get("primary") is True), None)
    return (primary or verified[0])["email"].strip()


def _get_repository_owner_email(token: str, repository_full_name: str) -> str:
    """Resolve the actual repository owner's email without guessing or using UI input.

    Personal repositories:
      - If the PAT belongs to the repository owner, use that account's primary
        verified email from GET /user/emails.
      - If the PAT belongs to a collaborator, use only the owner's public
        GitHub-profile email. GitHub does not expose another user's private email.

    Organization repositories:
      - Require the PAT account to be an active organization owner (role=admin),
        then use that owner's primary verified email.
    """
    secret = (token or "").strip()
    full_name = (repository_full_name or "").strip()
    if not secret:
        raise PermissionError("A GitHub PAT is required to resolve the repository owner email.")

    parts = full_name.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError("Repository must be supplied as owner/repository.")
    requested_owner, requested_repo = parts

    repository_data = _github_json(
        f"https://api.github.com/repos/{quote(requested_owner)}/{quote(requested_repo)}",
        token=secret,
    )
    if repository_data.get("private") is not True:
        raise PermissionError("The selected repository is not private.")

    owner_data = repository_data.get("owner") or {}
    owner_login = owner_data.get("login")
    owner_type = (owner_data.get("type") or "").strip().lower()
    if not owner_login or owner_type not in {"user", "organization"}:
        raise RuntimeError("GitHub did not return a supported repository-owner identity.")

    authenticated_user = _github_json("https://api.github.com/user", token=secret)
    authenticated_login = (authenticated_user.get("login") or "").strip()
    if not authenticated_login:
        raise PermissionError("GitHub could not identify the account associated with this PAT.")

    if owner_type == "user":
        if authenticated_login.casefold() == owner_login.casefold():
            # The PAT belongs to the repository owner. Prefer a verified account email.
            try:
                return _get_verified_pat_email(secret)
            except PermissionError as email_error:
                # A public profile email is an acceptable fallback only if GitHub
                # explicitly publishes it; never substitute a collaborator's email.
                try:
                    profile = _github_json(
                        f"https://api.github.com/users/{quote(owner_login)}", token=secret
                    )
                except Exception:
                    raise email_error
                public_email = profile.get("email") if isinstance(profile, dict) else None
                if _is_valid_github_email(public_email):
                    return public_email.strip()
                raise email_error

        # The PAT belongs to a collaborator, not the personal repository owner.
        # Only use an email the owner has explicitly made public on GitHub.
        owner_profile = _github_json(
            f"https://api.github.com/users/{quote(owner_login)}", token=secret
        )
        public_email = owner_profile.get("email") if isinstance(owner_profile, dict) else None
        if _is_valid_github_email(public_email):
            return public_email.strip()
        raise PermissionError(
            f"The entered PAT belongs to @{authenticated_login}, but the repository is owned by "
            f"@{owner_login}. GitHub does not disclose that owner's private email to collaborators. "
            "Use a PAT belonging to the repository owner with email-read permission, or ask the "
            "owner to make a contact email public on their GitHub profile. No email was sent."
        )

    # Organization-owned repository: a member PAT is not enough to identify an
    # authorized approver. Verify that the PAT account is an active org owner.
    try:
        membership = _github_json(
            f"https://api.github.com/user/memberships/orgs/{quote(owner_login)}",
            token=secret,
        )
    except Exception:
        raise PermissionError(
            f"{owner_login} is an organization. To send approval to an organization owner, "
            "the PAT account must be an active organization owner and the token must be able "
            "to read organization membership (classic PAT: read:org; fine-grained PAT: "
            "Members organization permission read). GitHub does not expose private emails "
            "of other organization owners. No email was sent."
        ) from None

    if membership.get("state") != "active" or membership.get("role") != "admin":
        raise PermissionError(
            f"The PAT account @{authenticated_login} is not an active owner of organization "
            f"{owner_login}. A repository member/collaborator cannot retrieve an owner's private "
            "email through GitHub. Use an active organization-owner PAT. No email was sent."
        )

    return _get_verified_pat_email(secret)


def _latest_file_commit(owner: str, repo: str, path: str, token: Optional[str] = None):
    """
    Return the latest commit SHA and committer timestamp affecting a file on
    the repository's default branch. Metadata failures do not block retrieval.
    """
    base = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
    encoded_path = quote(path, safe="/")
    url = f"{base}/commits?path={encoded_path}&per_page=1"
    try:
        payload = _github_json(url, token=token)
        if not isinstance(payload, list) or not payload:
            return None, None

        latest = payload[0]
        sha = latest.get("sha") if isinstance(latest, dict) else None
        commit = latest.get("commit", {}) if isinstance(latest, dict) else {}
        committer = commit.get("committer") or {}
        author = commit.get("author") or {}
        timestamp = committer.get("date") or author.get("date")
        return sha, timestamp
    except Exception:
        # Keep the file usable, but let the UI correctly report freshness as unknown.
        return None, None


def _attach_commit_metadata(owner: str, repo: str, file_item: Dict[str, Any],
                            token: Optional[str] = None) -> Dict[str, Any]:
    sha, timestamp = _latest_file_commit(owner, repo, file_item["path"], token)
    return {
        **file_item,
        "commit_sha": sha,
        "commit_timestamp": timestamp,
    }


def _github_raw_text(url: str) -> str:
    request = Request(
        url,
        headers={"User-Agent": "FreshTrust-MCP", "Accept": "text/plain"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=settings.github_mcp_timeout_seconds) as response:
            return response.read(settings.max_file_chars + 1).decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code == 404:
            return ""
        raise RuntimeError(f"Public file download failed with HTTP {exc.code}.") from None
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not download public repository file ({type(exc).__name__}).") from None


def _retrieve_public_sync(owner: str, repo: str, question: str, max_files: int):
    """
    Anonymous public repository retrieval. This code path does not launch Docker,
    initialize an MCP OAuth provider, or request any credential.
    """
    base = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
    repository = _github_json(base)
    branch = repository.get("default_branch")
    if not branch:
        raise RuntimeError("GitHub did not provide a default branch.")

    tree_payload = _github_json(
        f"{base}/git/trees/{quote(branch, safe='')}?recursive=1"
    )
    tree = tree_payload.get("tree", [])
    if not isinstance(tree, list):
        raise RuntimeError("GitHub returned an invalid repository tree.")

    question_terms = {
        term.lower() for term in re.findall(r"[A-Za-z0-9_]{3,}", question or "")
    }
    candidates = []
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        if not isinstance(path, str) or not path:
            continue
        if any(part.lower() in IGNORED_PARTS for part in path.split("/")):
            continue
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if suffix in SUPPORTED_SUFFIXES:
            candidates.append(path)

    if not candidates:
        raise RuntimeError(f"No supported source files were found in {owner}/{repo}.")

    ranked_paths = []
    for path in candidates:
        lower = path.lower()
        score = sum(1 for term in question_terms if term in lower)
        if lower in {"readme.md", "pyproject.toml", "requirements.txt", "package.json"}:
            score += 5
        elif lower.endswith(("/readme.md", "/main.py", "/app.py")):
            score += 3
        ranked_paths.append((score, path))

    ranked_paths.sort(key=lambda item: (-item[0], item[1].count("/"), item[1].lower()))
    selected_paths = [path for _, path in ranked_paths[:max_files]]

    raw_root = (
        f"https://raw.githubusercontent.com/{quote(owner)}/{quote(repo)}/"
        f"{quote(branch, safe='')}"
    )

    def fetch(path):
        encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
        content = _github_raw_text(f"{raw_root}/{encoded_path}")
        # Public source text is data; do not run MCP OAuth-challenge detection on it.
        content = (content[:settings.max_file_chars] or "").strip()
        if not content:
            return None
        return _attach_commit_metadata(
            owner, repo, {"path": path, "content": content}
        )

    # Bound concurrent file downloads and per-file commit metadata requests.
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(selected_paths)))) as pool:
        files = [item for item in pool.map(fetch, selected_paths) if item]

    if not files:
        raise RuntimeError(f"No readable files could be retrieved from public repository {owner}/{repo}.")
    return files

