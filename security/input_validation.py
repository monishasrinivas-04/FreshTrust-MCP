import re
from urllib.parse import urlparse

def parse_github_repo_url(value: str):
    parsed = urlparse((value or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        raise ValueError("Use a valid HTTPS GitHub repository URL.")
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) != 2:
        raise ValueError("Enter the repository root URL, not an issue, pull request or file URL.")
    owner, repo = parts
    repo = re.sub(r"\.git$", "", repo)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repo):
        raise ValueError("Invalid GitHub owner or repository name.")
    return owner, repo

def validate_repo_path(path):
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise ValueError("Invalid repository file path.")
    if any(part in {"..", "."} for part in path.split("/")):
        raise ValueError("Path traversal is not allowed.")
    if len(path) > 500:
        raise ValueError("Repository path is too long.")
    return path
