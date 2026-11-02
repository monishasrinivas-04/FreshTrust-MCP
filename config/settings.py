
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    github_mcp_url: str = os.getenv(
        "GITHUB_MCP_URL",
        "https://api.githubcopilot.com/mcp/"
    )
    cloudflare_account_id: str = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    cloudflare_api_token: str = os.getenv("CLOUDFLARE_API_TOKEN", "")
    cloudflare_model: str = os.getenv(
        "CLOUDFLARE_MODEL",
        "@cf/qwen/qwen3.8-27b"
    )
    embedding_model: str = os.getenv(
        "EMBEDDING_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2"
    )
    cache_path: str = os.getenv(
        "FRESHTRUST_CACHE_PATH",
        "data/freshtrust_cache.sqlite3"
    )
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "300"))
    max_files: int = int(os.getenv("MAX_FILES", "6"))
    max_file_chars: int = int(os.getenv("MAX_FILE_CHARS", "100000"))
    max_context_chars: int = int(os.getenv("MAX_CONTEXT_CHARS", "18000"))
    semantic_weight: float = float(os.getenv("SEMANTIC_WEIGHT", "0.75"))
    freshness_weight: float = float(os.getenv("FRESHNESS_WEIGHT", "0.25"))


settings = Settings()
