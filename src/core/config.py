import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _get(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    groq_api_key: str
    jwt_secret: str
    database_path: Path
    primary_base_url: str
    backup_base_url: str
    primary_model: str
    backup_model: str
    rate_limit_tokens_per_minute: int
    upstream_timeout_ms: int
    log_level: str
    mcp_gateway_url: str


def load_settings() -> Settings:
    raw_path = Path(_get("DATABASE_PATH", "data/gateway.db"))
    database_path = raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path
    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
        jwt_secret=_get("JWT_SECRET", "change-me-in-production"),
        database_path=database_path,
        primary_base_url=_get("PRIMARY_BASE_URL", "https://api.openai.com/v1"),
        backup_base_url=_get("BACKUP_BASE_URL", "https://api.groq.com/openai/v1"),
        primary_model=_get("PRIMARY_MODEL", "gpt-4o-mini"),
        backup_model=_get("BACKUP_MODEL", "llama-3.1-8b-instant"),
        rate_limit_tokens_per_minute=_get_int("RATE_LIMIT_TOKENS_PER_MINUTE", 50000),
        upstream_timeout_ms=_get_int("UPSTREAM_TIMEOUT_MS", 3000),
        log_level=_get("LOG_LEVEL", "INFO"),
        # Inside a container 127.0.0.1 is that container itself, so compose
        # overrides this with the service name. The default suits running the
        # two services directly on one machine.
        mcp_gateway_url=_get("MCP_GATEWAY_URL", "http://127.0.0.1:8000"),
    )


settings = load_settings()
