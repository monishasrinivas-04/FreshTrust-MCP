ALLOWED_TOOL_NAMES = {
    "search_code", "get_file_contents", "get_file_content",
    "search_repositories", "list_commits"
}

def authorize_tool(tool_name, arguments, owner, repo):
    if tool_name not in ALLOWED_TOOL_NAMES:
        raise PermissionError(f"MCP tool is not allow-listed: {tool_name}")
    if arguments.get("owner", owner) != owner or arguments.get("repo", repo) != repo:
        raise PermissionError("MCP tool arguments attempted to escape the selected repository.")
    path = arguments.get("path") or arguments.get("file_path")
    if path:
        from security.input_validation import validate_repo_path
        validate_repo_path(path)
