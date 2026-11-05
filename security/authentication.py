"""Authentication policy for GitHub MCP.

This module deliberately performs no GitHub REST requests. The MCP gateway
validates the credential when a repository operation is executed.
"""


def validate_github_access(owner, repo, token=None, private=False):
    if not owner or not repo:
        raise ValueError("A repository owner and name are required.")
    if private and not (token or "").strip():
        raise PermissionError("A GitHub PAT is required for a private repository.")
    # Credential validity and repository permissions are verified by MCP itself.
    return True
