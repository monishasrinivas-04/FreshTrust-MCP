"""FreshTrust-MCP configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    # Official local GitHub MCP Server is used for private repositories. Legacy OAuth settings remain for compatibility but are not used for public retrieval.
    github_mcp_docker_command: str = os.getenv("GITHUB_MCP_DOCKER_COMMAND", "docker")
    github_mcp_image: str = os.getenv(
        "GITHUB_MCP_IMAGE", "ghcr.io/github/github-mcp-server:latest"
    )
    github_mcp_toolsets: str = os.getenv(
        "GITHUB_MCP_TOOLSETS", "context,repos,users"
    )
    github_mcp_oauth_callback_port: int = int(
        os.getenv("GITHUB_MCP_OAUTH_CALLBACK_PORT", "8085")
    )
    # Leave OAuth scopes to the official server default. Narrow scopes can hide tools.
    github_mcp_oauth_scopes: str = os.getenv("GITHUB_MCP_OAUTH_SCOPES", "")
    github_mcp_timeout_seconds: int = int(
        os.getenv("GITHUB_MCP_TIMEOUT_SECONDS", "90")
    )

    cloudflare_account_id: str = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    cloudflare_api_token: str = os.getenv("CLOUDFLARE_API_TOKEN", "")
    cloudflare_model: str = os.getenv("CLOUDFLARE_MODEL", "@cf/qwen/qwen3.8-27b")

    embedding_model: str = os.getenv(
        "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
    )
    database_url: str = os.getenv("DATABASE_URL", "")
    cache_path: str = os.getenv(
        "FRESHTRUST_CACHE_PATH", "data/freshtrust_cache.sqlite3"
    )
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "300"))
    max_files: int = int(os.getenv("MAX_FILES", "6"))
    max_file_chars: int = int(os.getenv("MAX_FILE_CHARS", "100000"))
    max_context_chars: int = int(os.getenv("MAX_CONTEXT_CHARS", "18000"))
    semantic_weight: float = float(os.getenv("SEMANTIC_WEIGHT", "0.75"))
    freshness_weight: float = float(os.getenv("FRESHNESS_WEIGHT", "0.25"))


settings = Settings()
