"""Independent BM25 and dense recall with deterministic RRF fusion."""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any
from urllib.request import Request, urlopen

from config import settings
from service.core.context import pack_context
from service.core.tokenization import tokenize_text
from service.core.types import Evidence, RecallHit

logger = logging.getLogger(__name__)

SOURCE_FIELDS = [
    "chunk_id",
    "document_id",
    "document_name",
    "content",
    "chunk_hash",
    "chunk_index",
    "block_type",
    "heading_path",
    "page",
    "positions",
]


class RetrievalUnavailable(RuntimeError):
    pass


def _es_client():
    from service.core.rag.utils.es_conn import ESConnection

    return ESConnection().es


def _generate_query_embedding(question: str):
    from service.core.rag.nlp.model import generate_embedding

    return generate_embedding(
        question,
        model_name=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )


def _scope_filter(user_id: str, session_id: str | None) -> dict[str, Any]:
    choices: list[dict[str, Any]] = [
        {"bool": {"filter": [{"term": {"user_id": user_id}}, {"term": {"scope": "user"}}]}}
    ]
    if session_id:
        choices.append(
            {
                "bool": {
                    "filter": [
                        {"term": {"user_id": user_id}},
                        {"term": {"scope": "session"}},
                        {"term": {"session_id": session_id}},
                        {"range": {"expires_at": {"gt": "now"}}},
                    ]
                }
            }
        )
    return {"bool": {"should": choices, "minimum_should_match": 1}}


def _hits(response: dict[str, Any], branch: str) -> list[RecallHit]:
    result: list[RecallHit] = []
    for rank, hit in enumerate(response.get("hits", {}).get("hits", []), start=1):
        source = hit.get("_source") or {}
        result.append(
            RecallHit(
                chunk_id=str(hit.get("_id") or source.get("chunk_id")),
                source=source,
                rank=rank,
                score=float(hit.get("_score") or 0.0),
                branch=branch,
            )
        )
    return result


def bm25_recall(user_id: str, session_id: str | None, question: str) -> list[RecallHit]:
    """Run lexical recall only; no vector query or script score is present."""

    query_tokens = tokenize_text(question)
    response = _es_client().search(
        index=settings.rag_index_name,
        body={
            "size": settings.recall_k,
            "_source": SOURCE_FIELDS,
            "query": {
                "bool": {
                    "filter": [_scope_filter(user_id, session_id)],
                    "must": [
                        {
                            "multi_match": {
                                "query": query_tokens,
                                "fields": ["title_tokens^2", "heading_tokens^1.5", "content_tokens"],
                                "type": "best_fields",
                            }
                        }
                    ],
                }
            },
        },
    )
    return _hits(response, "bm25")


def dense_recall(user_id: str, session_id: str | None, question: str) -> list[RecallHit]:
    """Run vector recall independently with ownership filters only."""

    vector = _generate_query_embedding(question)
    if not vector:
        raise RetrievalUnavailable("Query embedding generation failed")
    response = _es_client().search(
        index=settings.rag_index_name,
        body={
            "size": settings.recall_k,
            "_source": SOURCE_FIELDS,
            "knn": {
                "field": "embedding",
                "query_vector": vector,
                "k": settings.recall_k,
                "num_candidates": max(100, settings.recall_k * 2),
                "filter": _scope_filter(user_id, session_id),
            },
        },
    )
    return _hits(response, "dense")


def reciprocal_rank_fusion(branches: list[list[RecallHit]]) -> list[dict[str, Any]]:
    scores: dict[str, float] = defaultdict(float)
    best_rank: dict[str, int] = {}
    sources: dict[str, dict[str, Any]] = {}
    branch_names: dict[str, set[str]] = defaultdict(set)

    for branch in branches:
        for hit in branch:
            scores[hit.chunk_id] += 1.0 / (settings.rrf_k + hit.rank)
            best_rank[hit.chunk_id] = min(best_rank.get(hit.chunk_id, hit.rank), hit.rank)
            sources[hit.chunk_id] = hit.source
            branch_names[hit.chunk_id].add(hit.branch)

    ordered = sorted(scores, key=lambda key: (-scores[key], best_rank[key], key))
    return [
        {
            "chunk_id": chunk_id,
            "source": sources[chunk_id],
            "rrf_score": scores[chunk_id],
            "best_rank": best_rank[chunk_id],
            "retrieval_sources": sorted(branch_names[chunk_id]),
        }
        for chunk_id in ordered[: settings.fusion_k]
    ]


def _fingerprint(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", text or "")).lower()
    if len(normalized) < 3:
        return {normalized} if normalized else set()
    return {normalized[index : index + 3] for index in range(len(normalized) - 2)}


def _near_duplicate(left: set[str], right: set[str], threshold: float = 0.92) -> bool:
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= threshold


def deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_hashes: set[str] = set()
    fingerprints: list[set[str]] = []
    unique: list[dict[str, Any]] = []
    for candidate in candidates:
        source = candidate["source"]
        chunk_hash = str(source.get("chunk_hash") or "")
        if chunk_hash and chunk_hash in seen_hashes:
            continue
        fingerprint = _fingerprint(str(source.get("content") or ""))
        if any(_near_duplicate(fingerprint, existing) for existing in fingerprints):
            continue
        if chunk_hash:
            seen_hashes.add(chunk_hash)
        fingerprints.append(fingerprint)
        unique.append(candidate)
    return unique


def _rerank_text(candidate: dict[str, Any]) -> str:
    source = candidate["source"]
    heading = " > ".join(str(value) for value in (source.get("heading_path") or []))
    return (
        f"文件：{source.get('document_name') or ''}\n"
        f"章节：{heading}\n"
        f"正文：{source.get('content') or ''}"
    )


def _call_rerank_api(payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        settings.rerank_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=settings.rerank_timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def rerank_candidates(question: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rerank fused candidates with DashScope and degrade cleanly to RRF."""

    if not candidates:
        return []
    top_k = min(settings.rerank_top_k, len(candidates))
    fallback = candidates[:top_k]
    if not settings.rerank_enabled:
        return fallback

    payload = {
        "model": settings.rerank_model,
        "input": {
            "query": question,
            "documents": [_rerank_text(candidate) for candidate in candidates],
        },
        "parameters": {"return_documents": False, "top_n": top_k},
    }
    try:
        response = _call_rerank_api(payload)
        raw_results = response.get("output", {}).get("results", [])
        parsed: list[tuple[float, int, str]] = []
        for result in raw_results:
            index = int(result["index"])
            if index < 0 or index >= len(candidates):
                continue
            parsed.append(
                (
                    float(result.get("relevance_score") or 0.0),
                    index,
                    str(candidates[index]["chunk_id"]),
                )
            )
        parsed.sort(key=lambda item: (-item[0], item[1], item[2]))

        reranked: list[dict[str, Any]] = []
        seen_indexes: set[int] = set()
        for rank, (score, index, _chunk_id) in enumerate(parsed[:top_k], start=1):
            if index in seen_indexes:
                continue
            seen_indexes.add(index)
            candidate = dict(candidates[index])
            candidate["rerank_score"] = score
            candidate["rerank_rank"] = rank
            reranked.append(candidate)

        if not reranked:
            raise ValueError("rerank response contained no valid candidates")
        logger.info(
            "rerank model=%s input=%s output=%s",
            settings.rerank_model,
            len(candidates),
            len(reranked),
        )
        return reranked
    except Exception as exc:
        logger.warning("Rerank failed; falling back to RRF top %s: %s", top_k, exc)
        return fallback


def hybrid_retrieve(user_id: str, session_id: str | None, question: str) -> list[dict[str, Any]]:
    es = _es_client()
    if not es.indices.exists(index=settings.rag_index_name):
        raise RetrievalUnavailable(f"Retrieval index {settings.rag_index_name} is unavailable")

    branches: list[list[RecallHit]] = []
    failures: list[str] = []
    try:
        branches.append(bm25_recall(user_id, session_id, question))
    except Exception as exc:
        failures.append("bm25")
        logger.warning("BM25 recall failed: %s", exc)
    try:
        branches.append(dense_recall(user_id, session_id, question))
    except Exception as exc:
        failures.append("dense")
        logger.warning("Dense recall failed: %s", exc)

    if len(failures) == 2:
        raise RetrievalUnavailable("Both BM25 and dense retrieval failed")
    fused = reciprocal_rank_fusion(branches)
    logger.info(
        "retrieval branches=%s fused=%s degraded=%s",
        [len(branch) for branch in branches],
        len(fused),
        failures,
    )
    return deduplicate_candidates(fused)


def retrieve_evidence(user_id: str, session_id: str | None, question: str) -> list[Evidence]:
    candidates = hybrid_retrieve(user_id, session_id, question)
    return pack_context(rerank_candidates(question, candidates))


def retrieve_content(user_id: str, question: str, session_id: str | None = None) -> list[dict[str, Any]]:
    """Compatibility wrapper used by the existing route and history schema."""

    return [item.to_public_dict() for item in retrieve_evidence(user_id, session_id, question)]
