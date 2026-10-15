from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ParsedBlock:
    text: str
    block_type: str = "paragraph"
    heading_path: list[str] = field(default_factory=list)
    page: int | None = None
    table_id: str | None = None
    positions: list[list[int]] = field(default_factory=list)


@dataclass
class DocumentChunk:
    chunk_id: str
    document_id: str
    user_id: str
    session_id: str | None
    scope: str
    document_name: str
    content: str
    chunk_hash: str
    chunk_index: int
    block_type: str
    heading_path: list[str]
    page: int | None
    positions: list[list[int]]
    title_tokens: str
    heading_tokens: str
    content_tokens: str
    embedding: list[float]
    expires_at: str | None = None

    def to_index_document(self) -> dict[str, Any]:
        data = asdict(self)
        data["content_with_weight"] = data["content"]
        return data


@dataclass
class RecallHit:
    chunk_id: str
    source: dict[str, Any]
    rank: int
    score: float
    branch: str


@dataclass
class Evidence:
    citation_id: str
    chunk_id: str
    document_id: str
    document_name: str
    content: str
    page: int | None = None
    heading_path: list[str] = field(default_factory=list)
    block_type: str = "paragraph"
    positions: list[list[int]] = field(default_factory=list)
    retrieval_sources: list[str] = field(default_factory=list)
    rrf_score: float = 0.0
    rerank_score: float = 0.0
    token_count: int = 0

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.citation_id,
            "citation_id": self.citation_id,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_name": self.document_name,
            "content_with_weight": self.content,
            "page": self.page,
            "heading_path": self.heading_path,
            "block_type": self.block_type,
            "positions": self.positions,
            "retrieval_sources": self.retrieval_sources,
            "rerank_score": self.rerank_score,
        }
