"""Simplified configuration management."""

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


def _load_env_file() -> None:
    """Load .env from root directory if present."""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.is_file():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, value = stripped.split("=", 1)
                key, value = key.strip(), value.strip().strip("'\"")
                if key and value and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


_load_env_file()


DEFAULT_APP_NAME = "OpenPoke Server"
DEFAULT_APP_VERSION = "0.3.0"
DEFAULT_LLM_MODEL = os.getenv("OPENPOKE_MODEL", "anthropic/claude-sonnet-5.5")


def _env_int(name: str, fallback: int) -> int:
    try:
        return int(os.getenv(name, str(fallback)))
    except (TypeError, ValueError):
        return fallback


def _env_bool(name: str, fallback: bool = True) -> bool:
    return os.getenv(name, "1" if fallback else "0").strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, fallback: float) -> float:
    try:
        return float(os.getenv(name, str(fallback)))
    except (TypeError, ValueError):
        return fallback


class Settings(BaseModel):
    """Application settings with lightweight env fallbacks."""

    # App metadata
    app_name: str = Field(default=DEFAULT_APP_NAME)
    app_version: str = Field(default=DEFAULT_APP_VERSION)

    # Server runtime
    server_host: str = Field(default=os.getenv("OPENPOKE_HOST", "0.0.0.0"))
    server_port: int = Field(default=_env_int("OPENPOKE_PORT", 8001))

    # LLM model selection
    interaction_agent_model: str = Field(default=DEFAULT_LLM_MODEL)
    execution_agent_model: str = Field(default=DEFAULT_LLM_MODEL)
    execution_agent_search_model: str = Field(default=DEFAULT_LLM_MODEL)
    summarizer_model: str = Field(default=DEFAULT_LLM_MODEL)
    email_classifier_model: str = Field(default=DEFAULT_LLM_MODEL)

    # Credentials / integrations
    openrouter_api_key: Optional[str] = Field(default=os.getenv("OPENROUTER_API_KEY"))
    composio_gmail_auth_config_id: Optional[str] = Field(default=os.getenv("COMPOSIO_GMAIL_AUTH_CONFIG_ID"))
    composio_api_key: Optional[str] = Field(default=os.getenv("COMPOSIO_API_KEY"))

    # HTTP behaviour
    cors_allow_origins_raw: str = Field(default=os.getenv("OPENPOKE_CORS_ALLOW_ORIGINS", "*"))
    enable_docs: bool = Field(default=os.getenv("OPENPOKE_ENABLE_DOCS", "1") != "0")
    docs_url: Optional[str] = Field(default=os.getenv("OPENPOKE_DOCS_URL", "/docs"))

    # Summarisation controls
    conversation_summary_threshold: int = Field(default=100)
    conversation_summary_tail_size: int = Field(default=10)

    # Agent discovery and durable execution. Numerical limits are configurable;
    # none is a semantic-similarity confidence threshold.
    agent_registry_path: str = Field(default=os.getenv("OPENPOKE_AGENT_REGISTRY_PATH", str(Path(__file__).parent / "data" / "agents" / "registry.db")))
    agent_hybrid_search_enabled: bool = Field(default=_env_bool("OPENPOKE_AGENT_HYBRID_SEARCH"))
    agent_compaction_enabled: bool = Field(default=_env_bool("OPENPOKE_AGENT_COMPACTION"))
    agent_auto_archive_enabled: bool = Field(default=_env_bool("OPENPOKE_AGENT_AUTO_ARCHIVE"))
    agent_embedding_model: str = Field(default=os.getenv("OPENPOKE_AGENT_EMBEDDING_MODEL", "openai/text-embedding-3-small"))
    agent_embedding_dimensions: int = Field(default=_env_int("OPENPOKE_AGENT_EMBEDDING_DIMENSIONS", 1536), ge=1)
    agent_embedding_timeout_seconds: int = Field(default=_env_int("OPENPOKE_AGENT_EMBEDDING_TIMEOUT_SECONDS", 5), ge=1)
    agent_token_safety_margin: float = Field(default=_env_float("OPENPOKE_AGENT_TOKEN_SAFETY_MARGIN", 1.15), ge=1.0)
    agent_maintenance_interval_seconds: int = Field(default=_env_int("OPENPOKE_AGENT_MAINTENANCE_INTERVAL_SECONDS", 3600), ge=1)
    agent_archive_days: int = Field(default=_env_int("OPENPOKE_AGENT_ARCHIVE_DAYS", 30), ge=1)
    agent_max_concurrency: int = Field(default=_env_int("OPENPOKE_AGENT_MAX_CONCURRENCY", 4), ge=1)
    agent_queue_capacity: int = Field(default=_env_int("OPENPOKE_AGENT_QUEUE_CAPACITY", 100), ge=1)
    agent_queue_wait_seconds: int = Field(default=_env_int("OPENPOKE_AGENT_QUEUE_WAIT_SECONDS", 300), ge=1)
    agent_execution_timeout_seconds: int = Field(default=_env_int("OPENPOKE_AGENT_EXECUTION_TIMEOUT_SECONDS", 300), ge=1)
    agent_shutdown_seconds: int = Field(default=_env_int("OPENPOKE_AGENT_SHUTDOWN_SECONDS", 30), ge=0)
    agent_candidate_pool: int = Field(default=_env_int("OPENPOKE_AGENT_CANDIDATE_POOL", 30), ge=1)
    agent_candidate_page_size: int = Field(default=_env_int("OPENPOKE_AGENT_CANDIDATE_PAGE_SIZE", 10), ge=1)
    agent_candidate_tokens: int = Field(default=_env_int("OPENPOKE_AGENT_CANDIDATE_TOKENS", 200), ge=20)
    agent_discovery_tokens: int = Field(default=_env_int("OPENPOKE_AGENT_DISCOVERY_TOKENS", 6000), ge=1)
    agent_inspection_tokens: int = Field(default=_env_int("OPENPOKE_AGENT_INSPECTION_TOKENS", 1500), ge=1)
    agent_history_tokens: int = Field(default=_env_int("OPENPOKE_AGENT_HISTORY_TOKENS", 8000), ge=1)
    agent_summary_tokens: int = Field(default=_env_int("OPENPOKE_AGENT_SUMMARY_TOKENS", 2000), ge=1)
    agent_excerpt_tokens: int = Field(default=_env_int("OPENPOKE_AGENT_EXCERPT_TOKENS", 2000), ge=1)
    agent_interaction_history_tokens: int = Field(default=_env_int("OPENPOKE_AGENT_INTERACTION_HISTORY_TOKENS", 8000), ge=1)
    agent_interaction_iterations: int = Field(default=_env_int("OPENPOKE_AGENT_INTERACTION_ITERATIONS", 12), ge=1)
    agent_execution_iterations: int = Field(default=_env_int("OPENPOKE_AGENT_EXECUTION_ITERATIONS", 8), ge=1)
    agent_discovery_calls: int = Field(default=_env_int("OPENPOKE_AGENT_DISCOVERY_CALLS", 6), ge=1)
    agent_max_inspections: int = Field(default=_env_int("OPENPOKE_AGENT_MAX_INSPECTIONS", 3), ge=1)
    agent_max_followup_stages: int = Field(default=_env_int("OPENPOKE_AGENT_MAX_FOLLOWUP_STAGES", 5), ge=0)
    agent_max_submissions: int = Field(default=_env_int("OPENPOKE_AGENT_MAX_SUBMISSIONS", 20), ge=1)

    @property
    def cors_allow_origins(self) -> List[str]:
        """Parse CORS origins from comma-separated string."""
        if self.cors_allow_origins_raw.strip() in {"", "*"}:
            return ["*"]
        return [origin.strip() for origin in self.cors_allow_origins_raw.split(",") if origin.strip()]

    @property
    def resolved_docs_url(self) -> Optional[str]:
        """Return documentation URL when docs are enabled."""
        return (self.docs_url or "/docs") if self.enable_docs else None

    @property
    def summarization_enabled(self) -> bool:
        """Flag indicating conversation summarisation is active."""
        return self.conversation_summary_threshold > 0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
