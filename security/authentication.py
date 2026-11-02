import requests

def validate_github_access(owner, repo, token, private=False):
    if private and not token:
        raise PermissionError("A GitHub token is required for a private repository.")
    if not token:
        return {"authenticated": False, "authorized": True, "visibility": "public"}
    headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {token}"}
    response=requests.get(f"https://api.github.com/repos/{owner}/{repo}",headers=headers,timeout=15)
    if response.status_code == 401:
        raise PermissionError("GitHub rejected the supplied token.")
    if response.status_code == 404:
        raise PermissionError("Repository not found or token is not authorized to access it.")
    if response.status_code == 403:
        if response.headers.get("X-RateLimit-Remaining") == "0":
            raise RuntimeError("GITHUB_RATE_LIMIT")
        raise PermissionError("GitHub denied repository access.")
    response.raise_for_status()
    data=response.json()
    return {"authenticated": True, "authorized": True,
            "visibility": "private" if data.get("private") else "public"}
