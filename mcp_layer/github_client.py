
"""
GitHub repository retrieval exclusively through GitHub MCP.

Public repositories:
  - Do not require a user-entered PAT.
  - Attempt MCP without a user token.
  - An optional server-side GITHUB_MCP_TOKEN may authenticate the MCP gateway.

Private repositories:
  - Require a user PAT.
  - Pass it to the MCP gateway; GitHub enforces repository authorization.

This module intentionally contains NO direct GitHub REST/HTTP repository-fetching code.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from config.settings import settings
from mcp_layer.response_parser import extract_mcp_text
from security.authorization import authorize_tool


log = logging.getLogger(__name__)


DEFAULT_CANDIDATE_PATHS = (
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "main.py",
    "app.py",
    "src/main.py",
)

SUPPORTED_SUFFIXES = {
    ".py", ".md", ".txt", ".rst", ".toml", ".yaml", ".yml",
    ".json", ".js", ".jsx", ".ts", ".tsx", ".sql", ".sh",
    ".ipynb", ".java", ".c", ".cpp", ".h", ".html", ".css",
}

IGNORED_PARTS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", ".next", ".idea",
}

STATUS_ONLY_MARKERS = (
    "successfully downloaded text file",
    "successfully retrieved file",
)


def _validate_repo(owner: str, repo: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner or ""):
        raise ValueError("Invalid GitHub owner.")

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo or ""):
        raise ValueError("Invalid GitHub repository name.")


def _normalise_visibility(visibility: Optional[str]) -> str:
    value = (visibility or "Private").strip().lower()

    if value in {"public", "private"}:
        return value

    raise ValueError(
        "visibility must be either 'Public' or 'Private'."
    )


def _select_tool(
    tools: Iterable[Any],
    exact_names: Iterable[str],
) -> Optional[Any]:
    by_name = {
        tool.name.lower(): tool
        for tool in tools
    }

    for name in exact_names:
        if name.lower() in by_name:
            return by_name[name.lower()]

    return None


def _build_tool_args(
    tool: Any,
    owner: str,
    repo: str,
    question: str = "",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Construct arguments using only parameters advertised by the MCP tool.

    Optional parameters such as 'fields' are omitted unless explicitly
    supported by a dedicated implementation. This avoids sending invalid
    enum values to the MCP server.
    """

    schema = (
        getattr(tool, "inputSchema", None)
        or getattr(tool, "input_schema", None)
        or {}
    )

    properties = schema.get("properties", {}) or {}
    args: Dict[str, Any] = {}

    for name in properties:
        key = name.lower()

        if key in {
            "owner",
            "username",
            "repository_owner",
            "org",
        }:
            args[name] = owner

        elif key in {
            "repo",
            "repository",
            "repository_name",
        }:
            args[name] = repo

        elif key in {
            "path",
            "file_path",
            "filepath",
            "file_name",
            "filename",
        }:
            if path is not None:
                args[name] = path

        elif key in {
            "query",
            "q",
            "search_query",
            "searchquery",
            "query_string",
        }:
            query = f"repo:{owner}/{repo}"

            if question.strip():
                query += f" {question.strip()}"

            args[name] = query

        elif key in {
            "per_page",
            "perpage",
            "limit",
            "page_size",
            "max_results",
        }:
            args[name] = 30

        elif key == "page":
            args[name] = 1

        elif key == "order":
            args[name] = "desc"

        # IMPORTANT:
        # Do not automatically populate 'fields'.
        # Its allowed enum values differ between MCP tools.

    missing = [
        key
        for key in (schema.get("required") or [])
        if key not in args
    ]

    if missing:
        raise RuntimeError(
            f"Cannot safely construct required arguments for MCP tool "
            f"{tool.name!r}: {missing}. "
            f"Advertised schema: {schema}"
        )

    # Preserve the existing project authorization policy.
    authorize_tool(tool.name, args, owner, repo)

    return args


def _walk_for_paths(value: Any, found: List[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():

            if (
                key.lower() in {
                    "path",
                    "file_path",
                    "filename",
                }
                and isinstance(child, str)
            ):
                found.append(child)

            else:
                _walk_for_paths(child, found)

    elif isinstance(value, list):
        for child in value:
            _walk_for_paths(child, found)


def _extract_paths(text: str) -> List[str]:
    """
    Extract repository file paths from JSON or text returned by MCP tools.
    """

    paths: List[str] = []

    try:
        payload = json.loads(text)
        _walk_for_paths(payload, paths)

    except (json.JSONDecodeError, TypeError):
        pass

    patterns = (
        r'"(?:path|file_path|filename)"\s*:\s*"([^"]+)"',

        r'(?<![\w.-])(?:[\w.-]+/)*[\w.-]+\.(?:'
        r'py|md|txt|rst|toml|yaml|yml|json|js|jsx|ts|tsx|'
        r'sql|java|c|cpp|h|ipynb'
        r')',
    )

    for pattern in patterns:
        paths.extend(
            re.findall(
                pattern,
                text,
                flags=re.IGNORECASE,
            )
        )

    result: List[str] = []
    seen = set()

    for path in paths:
        path = path.strip().strip("`'\"").lstrip("/")

        if not path or path in seen:
            continue

        if any(
            part in IGNORED_PARTS
            for part in path.split("/")
        ):
            continue

        suffix = (
            "." + path.rsplit(".", 1)[-1].lower()
            if "." in path
            else ""
        )

        if suffix and suffix not in SUPPORTED_SUFFIXES:
            continue

        seen.add(path)
        result.append(path)

    return result


def _clean_file_text(text: str) -> str:
    """
    Remove MCP download-status preambles without discarding source content.
    """

    value = (text or "").strip()

    if not value:
        return ""

    lines = value.splitlines()

    while lines and any(
        marker in lines[0].lower()
        for marker in STATUS_ONLY_MARKERS
    ):
        lines.pop(0)

    return "\n".join(lines).strip()


def _mcp_text(result: Any) -> str:
    """
    Extract readable text from MCP text and embedded-resource responses.
    """

    try:
        return extract_mcp_text(result) or ""

    except Exception as exc:
        raise RuntimeError(
            f"Could not parse the GitHub MCP response: {exc}"
        ) from exc


class GitHubMCPClient:
    """
    Single retrieval interface for public and private repositories.

    Existing call compatibility:

        client = GitHubMCPClient(token)

        files = await client.retrieve(
            owner,
            repo,
            question,
            max_files=6,
            visibility="Public",
        )
    """

    def __init__(self, token: Optional[str] = None):
        self.token = (token or "").strip() or None

    def _headers_for(self, visibility: str) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
        }

        if visibility == "private":
            if not self.token:
                raise PermissionError(
                    "A GitHub Personal Access Token is required "
                    "for a private repository."
                )

            headers["Authorization"] = f"Bearer {self.token}"

            return headers

        # Public repository:
        # No user-entered PAT is required.
        # A server-side gateway token is optional.
        gateway_token = os.getenv(
            "GITHUB_MCP_TOKEN",
            "",
        ).strip()

        if gateway_token:
            headers["Authorization"] = (
                f"Bearer {gateway_token}"
            )

        return headers

    async def retrieve(
        self,
        owner: str,
        repo: str,
        question: str = "",
        max_files: int = 6,
        visibility: str = "Private",
    ) -> List[Dict[str, str]]:

        _validate_repo(owner, repo)

        visibility_value = _normalise_visibility(
            visibility
        )

        if (
            not isinstance(max_files, int)
            or isinstance(max_files, bool)
            or max_files < 1
        ):
            raise ValueError(
                "max_files must be a positive integer."
            )

        max_files = min(max_files, 30)

        headers = self._headers_for(
            visibility_value
        )

        found_paths: List[str] = []

        log.info(
            "Starting GitHub MCP retrieval for %s/%s",
            owner,
            repo,
        )

        log.info(
            "Repository visibility: %s",
            visibility_value,
        )

        try:
            async with streamablehttp_client(
                settings.github_mcp_url,
                headers=headers,
            ) as (
                read_stream,
                write_stream,
                _,
            ):

                async with ClientSession(
                    read_stream,
                    write_stream,
                ) as session:

                    # 1. Initialize MCP session.
                    await session.initialize()

                    # 2. Discover available MCP tools.
                    tools = list(
                        (await session.list_tools()).tools
                    )

                    log.info(
                        "Discovered %d GitHub MCP tools.",
                        len(tools),
                    )

                    file_tool = _select_tool(
                        tools,
                        (
                            "get_file_contents",
                            "get_file_content",
                        ),
                    )

                    search_tool = _select_tool(
                        tools,
                        (
                            "search_code",
                            "code_search",
                        ),
                    )

                    if file_tool is None:
                        raise RuntimeError(
                            "GitHub MCP does not expose "
                            "get_file_contents. Available tools: "
                            + ", ".join(
                                tool.name
                                for tool in tools
                            )
                        )

                    log.info(
                        "Selected file retrieval tool: %s",
                        file_tool.name,
                    )

                    # 3. Search the repository for relevant paths.
                    if search_tool is not None and question.strip():

                        search_args = _build_tool_args(
                            search_tool,
                            owner,
                            repo,
                            question=question,
                        )

                        log.info(
                            "Calling MCP search tool: %s",
                            search_tool.name,
                        )

                        search_result = await session.call_tool(
                            search_tool.name,
                            arguments=search_args,
                        )

                        if getattr(
                            search_result,
                            "isError",
                            False,
                        ):
                            log.warning(
                                "GitHub MCP search failed: %s",
                                _mcp_text(search_result),
                            )

                        else:
                            search_text = _mcp_text(
                                search_result
                            )

                            found_paths.extend(
                                _extract_paths(search_text)
                            )

                            log.info(
                                "MCP search discovered %d paths.",
                                len(found_paths),
                            )

                    # 4. Use bounded candidate paths if search
                    # does not return usable file paths.
                    if not found_paths:
                        log.info(
                            "No paths discovered through search. "
                            "Using bounded candidate paths through MCP."
                        )

                        found_paths.extend(
                            DEFAULT_CANDIDATE_PATHS
                        )

                    # 5. Remove duplicate paths and enforce limit.
                    paths = list(
                        dict.fromkeys(found_paths)
                    )[:max_files]

                    log.info(
                        "Attempting retrieval of %d files.",
                        len(paths),
                    )

                    semaphore = asyncio.Semaphore(3)

                    async def fetch_one(
                        path: str,
                    ) -> Optional[Dict[str, str]]:

                        async with semaphore:

                            args = _build_tool_args(
                                file_tool,
                                owner,
                                repo,
                                path=path,
                            )

                            log.info(
                                "Fetching repository file through MCP: %s",
                                path,
                            )

                            result = await session.call_tool(
                                file_tool.name,
                                arguments=args,
                            )

                            if getattr(
                                result,
                                "isError",
                                False,
                            ):
                                error_text = _mcp_text(
                                    result
                                )

                                log.error(
                                    "MCP tool failed for %s. Error: %s",
                                    path,
                                    error_text or "No error details returned",
                                )

                                return None

                            content = _clean_file_text(
                                _mcp_text(result)
                            )

                            if not content:
                                log.warning(
                                    "MCP returned empty content for %s.",
                                    path,
                                )

                                return None

                            # Do not treat a status-only message
                            # as actual repository file content.
                            if any(
                                content.lower().startswith(marker)
                                for marker in STATUS_ONLY_MARKERS
                            ):
                                log.warning(
                                    "MCP returned a status message "
                                    "instead of file content for %s.",
                                    path,
                                )

                                return None

                            log.info(
                                "Successfully retrieved %s (%d characters).",
                                path,
                                len(content),
                            )

                            return {
                                "path": path,
                                "content": content,
                            }

                    # 6. Retrieve files concurrently with a limit
                    # of three simultaneous MCP requests.
                    results = await asyncio.gather(
                        *(
                            fetch_one(path)
                            for path in paths
                        ),
                        return_exceptions=True,
                    )

                    files: List[Dict[str, str]] = []

                    for path, result in zip(
                        paths,
                        results,
                    ):

                        if isinstance(result, dict):
                            files.append(result)

                        elif isinstance(
                            result,
                            BaseException,
                        ):
                            log.error(
                                "MCP retrieval failed for %s: %s",
                                path,
                                repr(result),
                            )

                    # 7. Fail clearly if no readable files were returned.
                    if not files:
                        raise RuntimeError(
                            f"GitHub MCP connected, but returned "
                            f"no readable files from {owner}/{repo}. "
                            f"Check the MCP tool arguments, repository "
                            f"permissions, and the detailed MCP errors "
                            f"logged above. No direct REST fallback was used."
                        )

                    log.info(
                        "GitHub MCP retrieval completed. "
                        "Successfully retrieved %d files.",
                        len(files),
                    )

                    return files

        except PermissionError:
            raise

        except Exception:
            # Preserve the original exception and traceback.
            # Do not mislabel every public-repository failure
            # as an authentication problem.
            log.exception(
                "GitHub MCP retrieval failed for %s/%s.",
                owner,
                repo,
            )

            raise
