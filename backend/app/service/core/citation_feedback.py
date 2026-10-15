"""Short-lived session feedback for citation-format mistakes."""

from __future__ import annotations

import hashlib
from functools import lru_cache

from redis import Redis
from redis.exceptions import RedisError

from config import settings
from utils import logger


def _warning_key(user_id: str, session_id: str) -> str:
    identity = hashlib.sha256(f"{user_id}:{session_id}".encode("utf-8")).hexdigest()
    return f"rag:citation-warning:{identity}"


@lru_cache(maxsize=1)
def _client() -> Redis:
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )


def has_citation_warning(user_id: str, session_id: str) -> bool:
    try:
        return bool(_client().get(_warning_key(user_id, session_id)))
    except RedisError as exc:
        logger.warning("Citation warning lookup failed: %s", exc)
        return False


def mark_citation_warning(user_id: str, session_id: str) -> None:
    try:
        _client().set(
            _warning_key(user_id, session_id),
            "1",
            ex=settings.citation_reminder_ttl_seconds,
        )
    except RedisError as exc:
        logger.warning("Citation warning write failed: %s", exc)


def clear_citation_warning(user_id: str, session_id: str) -> None:
    try:
        _client().delete(_warning_key(user_id, session_id))
    except RedisError as exc:
        logger.warning("Citation warning clear failed: %s", exc)
