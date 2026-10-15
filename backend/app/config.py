"""Central runtime configuration for the lightweight RAG pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        f"postgresql://postgres:{os.getenv('POSTGRES_PASSWORD', '')}@localhost:5432/gsk",
    )
    es_host: str = os.getenv("ES_HOST", "http://localhost:9201")
    es_password: str = os.getenv("ELASTIC_PASSWORD", "")
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "")

    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    chat_model: str = os.getenv("MODEL_NAME", os.getenv("LOCAL_CHAT_MODEL", "deepseek-v4-flash"))
    generation_temperature: float = _float("GENERATION_TEMPERATURE", 0.0)
    deepseek_thinking_enabled: bool = _bool("DEEPSEEK_THINKING_ENABLED", True)
    deepseek_reasoning_effort: str = os.getenv("DEEPSEEK_REASONING_EFFORT", "high").strip().lower()
    reasoning_display_max_chars: int = _int("DEEPSEEK_REASONING_DISPLAY_MAX_CHARS", 12000)

    dashscope_api_key: str = os.getenv("DASHSCOPE_API_KEY", "")
    dashscope_base_url: str = os.getenv(
        "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-v3")
    embedding_dimensions: int = _int("EMBEDDING_DIMENSIONS", 1024)

    rag_index_name: str = os.getenv("RAG_INDEX_NAME", "rag_chunks_v2")
    recall_k: int = _int("RAG_RECALL_K", 30)
    fusion_k: int = _int("RAG_FUSION_K", 20)
    rrf_k: int = _int("RAG_RRF_K", 60)
    rerank_enabled: bool = _bool("RAG_RERANK_ENABLED", True)
    rerank_model: str = os.getenv("RAG_RERANK_MODEL", "gte-rerank-v2")
    rerank_url: str = os.getenv(
        "RAG_RERANK_URL",
        "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
    )
    rerank_top_k: int = _int("RAG_RERANK_TOP_K", 8)
    rerank_timeout_seconds: int = _int("RAG_RERANK_TIMEOUT_SECONDS", 5)
    total_token_budget: int = _int("RAG_TOTAL_TOKEN_BUDGET", 12000)
    evidence_token_budget: int = _int("RAG_EVIDENCE_TOKEN_BUDGET", 8000)
    temp_document_ttl_hours: int = _int("RAG_TEMP_DOCUMENT_TTL_HOURS", 24)
    answer_validation_timeout: int = _int("LOCAL_ANSWER_VALIDATION_TIMEOUT_SECONDS", 60)
    answer_repair_timeout: int = _int("LOCAL_ANSWER_REPAIR_TIMEOUT_SECONDS", 60)
    history_turns: int = _int("RAG_HISTORY_TURNS", 4)
    history_token_budget: int = _int("RAG_HISTORY_TOKEN_BUDGET", 2000)
    redis_host: str = os.getenv("REDIS_HOST", "localhost")
    redis_port: int = _int("REDIS_PORT", 6379)
    redis_db: int = _int("REDIS_DB", 0)
    citation_reminder_ttl_seconds: int = _int("RAG_CITATION_REMINDER_TTL_SECONDS", 86400)


settings = Settings()


def validate_runtime_config() -> None:
    """Fail early with names only; never include secret values in errors."""

    required = {
        "DATABASE_URL": settings.database_url,
        "ELASTIC_PASSWORD": settings.es_password,
        "JWT_SECRET_KEY": settings.jwt_secret_key,
        "DASHSCOPE_API_KEY": settings.dashscope_api_key,
        "DEEPSEEK_API_KEY": settings.deepseek_api_key,
    }
    missing = [name for name, value in required.items() if not value]
    if not os.getenv("DATABASE_URL") and not os.getenv("POSTGRES_PASSWORD"):
        missing.append("DATABASE_URL or POSTGRES_PASSWORD")
    if missing:
        raise RuntimeError("Missing required configuration: " + ", ".join(missing))
    if settings.evidence_token_budget >= settings.total_token_budget:
        raise RuntimeError("RAG_EVIDENCE_TOKEN_BUDGET must be less than RAG_TOTAL_TOKEN_BUDGET")
    if settings.embedding_dimensions != 1024:
        raise RuntimeError("The rag_chunks_v2 mapping requires 1024-dimensional embeddings")
    if settings.rerank_top_k <= 0 or settings.rerank_timeout_seconds <= 0:
        raise RuntimeError("RAG rerank limits must be positive")
    if settings.deepseek_reasoning_effort not in {"high", "max"}:
        raise RuntimeError("DEEPSEEK_REASONING_EFFORT must be high or max")
    if settings.reasoning_display_max_chars < 0:
        raise RuntimeError("DEEPSEEK_REASONING_DISPLAY_MAX_CHARS must be non-negative")
    if settings.history_turns < 0 or settings.history_token_budget < 0:
        raise RuntimeError("RAG history limits must be non-negative")
    if settings.citation_reminder_ttl_seconds <= 0:
        raise RuntimeError("RAG_CITATION_REMINDER_TTL_SECONDS must be positive")
