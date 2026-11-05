
"""Least-privilege authorization for GitHub MCP retrieval."""

import re
from pathlib import PurePosixPath


READ_ONLY_TOOLS = {
    "get_file_contents",
    "search_code",
}

OWNER_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")

REPO_QUALIFIER_PATTERN = re.compile(
    r"(?<!\S)repo:([^\s]+)",
    re.IGNORECASE,
)

OTHER_SCOPE_PATTERN = re.compile(
    r"(?<!\S)(?:org|user):",
    re.IGNORECASE,
)


def validate_repository(owner, repo):
    if not isinstance(owner, str) or not OWNER_PATTERN.fullmatch(owner):
        raise PermissionError("Invalid repository owner.")

    if not isinstance(repo, str) or not REPO_PATTERN.fullmatch(repo):
        raise PermissionError("Invalid repository name.")


def validate_repository_path(path):
    if not isinstance(path, str) or not path:
        raise PermissionError("A repository file path is required.")

    if len(path) > 500:
        raise PermissionError("Repository path is too long.")

    if "\x00" in path or "\\" in path:
        raise PermissionError("Invalid repository path.")

    if path.startswith("/") or path.startswith("~"):
        raise PermissionError("Absolute paths are not permitted.")

    if re.search(r"%(?:2e|2f|5c)", path, re.IGNORECASE):
        raise PermissionError("Encoded path traversal is not permitted.")

    parts = PurePosixPath(path).parts

    if any(part in {".", ".."} for part in parts):
        raise PermissionError(
            "Repository path traversal is not permitted."
        )

    return True


def authorize_tool(tool_name, args, owner, repo):
    """
    Authorize an MCP invocation against the requested repository.

    Only get_file_contents and search_code are permitted.
    """

    validate_repository(owner, repo)

    if not isinstance(args, dict):
        raise PermissionError("Invalid MCP tool arguments.")

    if tool_name not in READ_ONLY_TOOLS:
        raise PermissionError(
            "MCP tool is not authorized for repository retrieval."
        )

    if tool_name == "get_file_contents":

        if set(args) != {"owner", "repo", "path"}:
            raise PermissionError(
                "Unexpected file retrieval arguments."
            )

        if (
            args.get("owner") != owner
            or args.get("repo") != repo
        ):
            raise PermissionError(
                "MCP request targets an unauthorized repository."
            )

        validate_repository_path(args.get("path"))
        return True

    if tool_name == "search_code":

        if set(args) != {"query"}:
            raise PermissionError(
                "Unexpected code search arguments."
            )

        query = args.get("query")

        if (
            not isinstance(query, str)
            or not query
            or len(query) > 256
        ):
            raise PermissionError(
                "Invalid GitHub code search query."
            )

        if "\n" in query or "\r" in query:
            raise PermissionError(
                "Invalid code search query."
            )

        if OTHER_SCOPE_PATTERN.search(query):
            raise PermissionError(
                "Additional repository scopes are not permitted."
            )

        qualifiers = REPO_QUALIFIER_PATTERN.findall(query)

        if len(qualifiers) != 1:
            raise PermissionError(
                "Code search must contain exactly one repository qualifier."
            )

        expected = f"{owner}/{repo}"

        if qualifiers[0].casefold() != expected.casefold():
            raise PermissionError(
                "Code search targets an unauthorized repository."
            )

        return True

    raise PermissionError("MCP tool is not authorized.")
