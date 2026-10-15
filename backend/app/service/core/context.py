"""Token-aware evidence packing with document diversity and stable citations."""

from __future__ import annotations

from config import settings
from service.core.ingestion import count_tokens
from service.core.types import Evidence


def pack_context(candidates: list[dict], token_budget: int | None = None) -> list[Evidence]:
    budget = token_budget or settings.evidence_token_budget
    selected: list[Evidence] = []
    used_tokens = 0

    for candidate in candidates:
        source = candidate["source"]
        content = str(source.get("content") or "").strip()
        if not content:
            continue
        heading_path = [str(value) for value in (source.get("heading_path") or [])]
        evidence_tokens = count_tokens(content) + count_tokens(" > ".join(heading_path)) + 16
        if evidence_tokens > budget or used_tokens + evidence_tokens > budget:
            continue
        selected.append(
            Evidence(
                citation_id="",
                chunk_id=str(candidate["chunk_id"]),
                document_id=str(source.get("document_id") or ""),
                document_name=str(source.get("document_name") or ""),
                content=content,
                page=source.get("page"),
                heading_path=heading_path,
                block_type=str(source.get("block_type") or "paragraph"),
                positions=source.get("positions") or [],
                retrieval_sources=list(candidate.get("retrieval_sources") or []),
                rrf_score=float(candidate.get("rrf_score") or 0.0),
                rerank_score=float(candidate.get("rerank_score") or 0.0),
                token_count=evidence_tokens,
            )
        )
        used_tokens += evidence_tokens

    for index, evidence in enumerate(selected, start=1):
        evidence.citation_id = f"C{index}"
    return selected


def render_evidence(evidence: list[Evidence]) -> str:
    sections: list[str] = []
    for item in evidence:
        metadata = [item.document_name]
        if item.heading_path:
            metadata.append(" > ".join(item.heading_path))
        if item.page is not None:
            metadata.append(f"page {item.page}")
        sections.append(
            f"<{item.citation_id} source={' | '.join(metadata)}>\n{item.content}\n</{item.citation_id}>"
        )
    return "\n\n".join(sections)
