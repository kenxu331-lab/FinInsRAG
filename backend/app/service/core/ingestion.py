"""Document ingestion: DeepDoc adapter, structure-aware chunks and ES indexing."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import tiktoken
from elasticsearch.helpers import bulk
from sqlalchemy.orm import Session

from config import settings
from models.document import Document
from service.core.tokenization import tokenize_text
from service.core.types import DocumentChunk, ParsedBlock

logger = logging.getLogger(__name__)

TARGET_CHUNK_TOKENS = 350
MAX_CHUNK_TOKENS = 550
CHUNK_OVERLAP_TOKENS = 50


def _es_client():
    from service.core.rag.utils.es_conn import ESConnection

    return ESConnection().es

try:
    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - tiktoken has a deterministic fallback below
    _ENCODER = None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENCODER is not None:
        return len(_ENCODER.encode(text, disallowed_special=()))
    return max(1, len(text) // 2)


def normalize_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def sha256_text(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def sha256_file(file_path: str) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _looks_like_heading(text: str) -> bool:
    value = normalize_text(text)
    if not value or len(value) > 80 or "\n" in text.strip():
        return False
    if re.search(r"[。！？.!?；;]$", value):
        return False
    return bool(
        re.match(
            r"^(#{1,6}\s+|第[一二三四五六七八九十百零〇\d]+[章节篇条]|"
            r"[一二三四五六七八九十百零〇]+[、.]|\d+(?:\.\d+){0,3}[、.\s])",
            value,
        )
        or (len(value) <= 24 and re.search(r"(条款|范围|责任|定义|材料|说明|附则|目录)$", value))
    )


def _block_type(text: str) -> str:
    stripped = text.strip()
    if re.search(r"<table\b|</tr>|</td>", stripped, re.IGNORECASE):
        return "table"
    lines = [line for line in stripped.splitlines() if line.strip()]
    if len(lines) >= 2 and sum("|" in line for line in lines) >= 2:
        return "table"
    if lines and all(re.match(r"^\s*(?:[-*•]|\d+[.)、])\s+", line) for line in lines[:3]):
        return "list"
    return "heading" if _looks_like_heading(stripped) else "paragraph"


def _heading_level_and_title(text: str) -> tuple[int, str] | None:
    value = text.strip()
    markdown_heading = re.match(r"^(#{1,6})\s*(.+?)\s*#*$", value)
    if markdown_heading:
        return len(markdown_heading.group(1)), markdown_heading.group(2).strip()
    if not _looks_like_heading(value):
        return None
    title = re.sub(r"^\s*\d+(?:\.\d+)*[、.\s]+", "", value).strip()
    if re.match(r"^第.+[章篇]", value):
        return 1, title
    if re.match(r"^第.+[节条]", value):
        return 2, title
    if re.match(r"^[一二三四五六七八九十百零〇]+[、.]", value):
        return 2, title
    numeric = re.match(r"^(\d+(?:\.\d+)*)[、.\s]", value)
    if numeric:
        return min(6, numeric.group(1).count(".") + 1), title
    return 1, value


def _is_markdown_table_line(text: str) -> bool:
    value = text.strip()
    return value.count("|") >= 2


def _is_list_line(text: str) -> bool:
    return bool(re.match(r"^\s*(?:[-*•]|\d+[.)、])\s+", text))


def _is_table_note(text: str) -> bool:
    return bool(re.match(r"^\s*(?:注|备注|说明|单位|数据口径|脚注)\s*[:：]", text, re.IGNORECASE))


def _is_table_caption(text: str) -> bool:
    return bool(re.match(r"^\s*(?:表|附表|table)\s*[\d一二三四五六七八九十]*", text, re.IGNORECASE))


_INLINE_STRUCTURE_PATTERN = re.compile(
    r"第[一二三四五六七八九十百零〇0-9]+[编篇章节条款]"
)
_PDF_POSITION_PATTERN = re.compile(
    r"@@([0-9-]+)\t([0-9.-]+)\t([0-9.-]+)\t([0-9.-]+)\t([0-9.-]+)##"
)


def _split_inline_structures(text: str) -> list[str]:
    """Split a glued chapter/article only after real sentence-ending punctuation."""

    starts = [0]
    for match in _INLINE_STRUCTURE_PATTERN.finditer(text):
        start = match.start()
        if start == 0:
            continue
        prefix = text[:start].rstrip()
        if prefix and prefix[-1] in "。；;！？!?":
            starts.append(start)
    starts = sorted(set(starts))
    return [
        text[start:end].strip()
        for start, end in zip(starts, starts[1:] + [len(text)])
        if text[start:end].strip()
    ]


def _can_use_layout_heading_hint(text: str) -> bool:
    value = normalize_text(text)
    return bool(
        value
        and len(value) <= 80
        and "\n" not in text.strip()
        and not re.search(r"[。！？.!?；;]$", value)
    )


def _split_structured_text(
    text: str,
    heading_stack: list[tuple[int, str]],
    *,
    page: int | None = None,
    positions: list[list[int]] | None = None,
    block_type_hint: str | None = None,
) -> list[ParsedBlock]:
    """Recover headings, lists and tables from one parser text payload."""

    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return []
    positions = positions or []
    if block_type_hint == "table" or re.search(r"<table\b", raw, re.IGNORECASE):
        return [
            ParsedBlock(
                text=raw,
                block_type="table",
                heading_path=[title for _, title in heading_stack],
                page=page,
                positions=positions,
            )
        ]

    blocks: list[ParsedBlock] = []
    buffer: list[str] = []
    buffer_type = "paragraph"

    def path() -> list[str]:
        return [title for _, title in heading_stack]

    def flush() -> None:
        nonlocal buffer, buffer_type
        if not buffer:
            return
        value = "\n".join(buffer).strip()
        if value:
            blocks.append(
                ParsedBlock(
                    text=value,
                    block_type=buffer_type,
                    heading_path=path(),
                    page=page,
                    positions=positions,
                )
            )
        buffer = []
        buffer_type = "paragraph"

    raw_lines = [part for line in raw.split("\n") for part in _split_inline_structures(line)]
    force_first_heading = block_type_hint == "heading"
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        heading = _heading_level_and_title(stripped)
        if heading is None and force_first_heading and _can_use_layout_heading_hint(stripped):
            heading = (1, stripped)
        force_first_heading = False
        if heading:
            flush()
            level, title = heading
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            blocks.append(
                ParsedBlock(
                    text=stripped,
                    block_type="heading",
                    heading_path=path(),
                    page=page,
                    positions=positions,
                )
            )
            continue
        line_type = (
            "table"
            if _is_markdown_table_line(stripped)
            else "list"
            if _is_list_line(stripped)
            else "paragraph"
        )
        if buffer and buffer_type != line_type:
            flush()
        buffer_type = line_type
        buffer.append(stripped)
    flush()

    # Keep table captions, units and footnotes with the table evidence itself.
    result: list[ParsedBlock] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.block_type == "table":
            if result and result[-1].block_type == "paragraph" and _is_table_caption(result[-1].text):
                caption = result.pop()
                block.text = caption.text + "\n" + block.text
            if index + 1 < len(blocks):
                following = blocks[index + 1]
                if following.block_type == "paragraph" and _is_table_note(following.text):
                    block.text += "\n\n" + following.text
                    index += 1
        result.append(block)
        index += 1
    return result


def _add_structured_blocks(
    output: list[ParsedBlock],
    text: str,
    heading_stack: list[tuple[int, str]],
    table_number: int,
    *,
    page: int | None = None,
    positions: list[list[int]] | None = None,
    block_type_hint: str | None = None,
) -> int:
    structured = _split_structured_text(
        text,
        heading_stack,
        page=page,
        positions=positions,
        block_type_hint=block_type_hint,
    )
    for block in structured:
        if block.block_type == "table":
            table_number += 1
            block.table_id = f"table-{table_number}"
        output.append(block)
    return table_number


def _parse_pdf_position_tag(tag: str) -> list[list[int]]:
    positions: list[list[int]] = []
    for match in _PDF_POSITION_PATTERN.finditer(tag or ""):
        pages, left, right, top, bottom = match.groups()
        for page in pages.split("-"):
            positions.append(
                [
                    int(page),
                    int(float(left)),
                    int(float(right)),
                    int(float(top)),
                    int(float(bottom)),
                ]
            )
    return positions


def _normalize_pdf_table_positions(raw_positions: object) -> list[list[int]]:
    positions: list[list[int]] = []
    for position in raw_positions or []:
        if not isinstance(position, (list, tuple)) or len(position) != 5:
            continue
        page, left, right, top, bottom = position
        positions.append(
            [
                int(page) + 1,
                int(float(left)),
                int(float(right)),
                int(float(top)),
                int(float(bottom)),
            ]
        )
    return positions


def _normalize_extracted_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, (list, tuple)):
        return "\n".join(str(item).strip() for item in content if str(item).strip())
    return str(content or "").strip()


def _parse_docx_blocks(file_path: str) -> list[ParsedBlock]:
    from service.core.rag.app.naive import Docx

    sections, tables = Docx()(file_path)
    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    table_number = 0

    for section in sections or []:
        text = str(section[0] if isinstance(section, (list, tuple)) else section).strip()
        if text:
            table_number = _add_structured_blocks(
                blocks, text, heading_stack, table_number
            )

    # The current DOCX parser returns tables separately and does not expose
    # their original body positions. Preserve its native order by appending
    # them after the paragraph stream, exactly as the parser returns them.
    for table in tables or []:
        try:
            content = table[0][1]
        except (IndexError, TypeError):
            continue
        text = _normalize_extracted_content(content)
        if text:
            table_number = _add_structured_blocks(
                blocks,
                text,
                heading_stack,
                table_number,
                block_type_hint="table",
            )
    return blocks


def _parse_pdf_blocks(file_path: str) -> list[ParsedBlock]:
    from service.core.rag.app.naive import Pdf

    parser = Pdf()
    sections, tables = parser(
        file_path,
        callback=lambda *_args, **_kwargs: None,
    )
    records: list[dict] = []
    boxes = list(getattr(parser, "boxes", []) or [])

    for order, section in enumerate(sections or []):
        if not isinstance(section, (list, tuple)) or not section:
            continue
        text = str(section[0] or "").strip()
        if not text:
            continue
        tag = str(section[1] or "") if len(section) > 1 else ""
        positions = _parse_pdf_position_tag(tag)
        box = boxes[order] if order < len(boxes) else {}
        layout_type = str(box.get("layout_type") or "").lower()
        page = positions[0][0] if positions else None
        top = positions[0][3] if positions else order
        records.append(
            {
                "sort": (page if page is not None else 10**9, top, 1, order),
                "text": text,
                "page": page,
                "positions": positions,
                "block_type_hint": "heading" if layout_type == "title" else None,
            }
        )

    table_order = len(records)
    for offset, table in enumerate(tables or []):
        try:
            content = table[0][1]
            raw_positions = table[1]
        except (IndexError, TypeError):
            continue
        text = _normalize_extracted_content(content)
        if not text:
            continue
        positions = _normalize_pdf_table_positions(raw_positions)
        page = positions[0][0] if positions else None
        top = positions[0][3] if positions else table_order + offset
        is_table = isinstance(content, str) and "<table" in content.lower()
        records.append(
            {
                "sort": (page if page is not None else 10**9, top, 0, table_order + offset),
                "text": text,
                "page": page,
                "positions": positions,
                "block_type_hint": "table" if is_table else None,
            }
        )

    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    table_number = 0
    for record in sorted(records, key=lambda item: item["sort"]):
        table_number = _add_structured_blocks(
            blocks,
            record["text"],
            heading_stack,
            table_number,
            page=record["page"],
            positions=record["positions"],
            block_type_hint=record["block_type_hint"],
        )
    return blocks


def _parse_legacy_blocks(file_path: str) -> list[ParsedBlock]:
    """Compatibility adapter for formats not yet exposed as raw parser blocks."""

    from service.core.rag.app.naive import chunk as deepdoc_chunk

    parsed = deepdoc_chunk(
        file_path,
        callback=lambda *_args, **_kwargs: None,
        parser_config={
            "chunk_token_num": TARGET_CHUNK_TOKENS,
            "delimiter": "\n!?;。；！？",
            "layout_recognize": "DeepDOC",
        },
    )
    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    table_number = 0

    for item in parsed or []:
        text = str(item.get("content_with_weight") or "").strip()
        if not text:
            continue
        raw_positions = item.get("position_int") or []
        positions = [list(position) for position in raw_positions if isinstance(position, (list, tuple))]
        pages = item.get("page_num_int") or []
        page = int(pages[0]) if pages else (int(positions[0][0]) if positions else None)
        structured = _split_structured_text(
            text,
            heading_stack,
            page=page,
            positions=positions,
        )
        for block in structured:
            if block.block_type == "table":
                table_number += 1
                block.table_id = f"table-{table_number}"
            blocks.append(block)
    return blocks


def parse_blocks(file_path: str) -> list[ParsedBlock]:
    """Parse raw DOCX/PDF blocks before structure-aware chunking."""

    suffix = Path(file_path).suffix.lower()
    if suffix == ".docx":
        return _parse_docx_blocks(file_path)
    if suffix == ".pdf":
        return _parse_pdf_blocks(file_path)
    return _parse_legacy_blocks(file_path)


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[。！？.!?；;])\s*|\n+", text) if part.strip()]


def _split_oversized_block(block: ParsedBlock) -> list[ParsedBlock]:
    if block.block_type in {"table", "list"} or count_tokens(block.text) <= MAX_CHUNK_TOKENS:
        return [block]

    sentences = _split_sentences(block.text)
    if len(sentences) <= 1:
        # An indivisible clause is preserved instead of being cut mid-sentence.
        return [block]

    result: list[ParsedBlock] = []
    current: list[str] = []
    current_tokens = 0
    for sentence in sentences:
        tokens = count_tokens(sentence)
        if current and current_tokens + tokens > MAX_CHUNK_TOKENS:
            result.append(
                ParsedBlock(
                    text="\n".join(current),
                    block_type=block.block_type,
                    heading_path=block.heading_path,
                    page=block.page,
                    table_id=block.table_id,
                    positions=block.positions,
                )
            )
            overlap: list[str] = []
            overlap_tokens = 0
            for previous in reversed(current):
                previous_tokens = count_tokens(previous)
                if overlap_tokens + previous_tokens > CHUNK_OVERLAP_TOKENS:
                    break
                overlap.insert(0, previous)
                overlap_tokens += previous_tokens
            current = overlap
            current_tokens = overlap_tokens
        current.append(sentence)
        current_tokens += tokens
    if current:
        result.append(
            ParsedBlock(
                text="\n".join(current),
                block_type=block.block_type,
                heading_path=block.heading_path,
                page=block.page,
                table_id=block.table_id,
                positions=block.positions,
            )
        )
    return result


def structure_aware_chunks(blocks: Iterable[ParsedBlock]) -> list[ParsedBlock]:
    """Merge adjacent prose while keeping tables, lists and sentences intact."""

    expanded: list[ParsedBlock] = []
    for block in blocks:
        expanded.extend(_split_oversized_block(block))

    output: list[ParsedBlock] = []
    pending: list[ParsedBlock] = []
    pending_tokens = 0

    def flush() -> None:
        nonlocal pending, pending_tokens
        if not pending:
            return
        first = pending[0]
        positions = [position for item in pending for position in item.positions]
        output.append(
            ParsedBlock(
                text="\n\n".join(item.text for item in pending),
                block_type="paragraph",
                heading_path=first.heading_path,
                page=first.page,
                positions=positions,
            )
        )
        pending = []
        pending_tokens = 0

    for block in expanded:
        if block.block_type == "heading":
            # The clean heading text stays in ParsedBlock while its path is
            # carried by answerable chunks and included in retrieval fields.
            continue
        tokens = count_tokens(block.text)
        standalone = block.block_type in {"table", "list"} or tokens > TARGET_CHUNK_TOKENS
        heading_changed = pending and pending[0].heading_path != block.heading_path
        would_overflow = pending and pending_tokens + tokens > MAX_CHUNK_TOKENS
        if standalone or heading_changed or would_overflow:
            flush()
        if standalone:
            output.append(block)
        else:
            pending.append(block)
            pending_tokens += tokens
            if pending_tokens >= TARGET_CHUNK_TOKENS:
                flush()
    flush()
    return output


def ensure_rag_index() -> None:
    es = _es_client()
    if es.indices.exists(index=settings.rag_index_name):
        return
    try:
        es.indices.create(
            index=settings.rag_index_name,
            mappings={
            "dynamic": "strict",
            "properties": {
                "chunk_id": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "user_id": {"type": "keyword"},
                "session_id": {"type": "keyword"},
                "scope": {"type": "keyword"},
                "document_name": {"type": "keyword"},
                "content": {"type": "text", "index": False},
                "content_with_weight": {"type": "text", "index": False},
                "chunk_hash": {"type": "keyword"},
                "chunk_index": {"type": "integer"},
                "block_type": {"type": "keyword"},
                "heading_path": {"type": "keyword"},
                "page": {"type": "integer"},
                "positions": {"type": "integer", "index": False},
                "title_tokens": {"type": "text", "analyzer": "whitespace", "similarity": "BM25"},
                "heading_tokens": {"type": "text", "analyzer": "whitespace", "similarity": "BM25"},
                "content_tokens": {"type": "text", "analyzer": "whitespace", "similarity": "BM25"},
                "embedding": {
                    "type": "dense_vector",
                    "dims": 1024,
                    "index": True,
                    "similarity": "cosine",
                },
                "expires_at": {"type": "date"},
            },
            },
        )
    except Exception:
        # Concurrent first uploads may race to create the same fixed index.
        if not es.indices.exists(index=settings.rag_index_name):
            raise


def _find_duplicate(
    db: Session, *, user_id: str, session_id: str | None, scope: str, content_hash: str
) -> Document | None:
    query = db.query(Document).filter(
        Document.user_id == user_id,
        Document.scope == scope,
        Document.content_hash == content_hash,
        Document.parse_status == "ready",
    )
    if scope == "session":
        query = query.filter(Document.session_id == session_id)
    return query.order_by(Document.created_at.asc()).first()


def _scoped_documents_query(
    db: Session, *, user_id: str, session_id: str | None, scope: str
):
    query = db.query(Document).filter(
        Document.user_id == user_id,
        Document.scope == scope,
    )
    if scope == "session":
        query = query.filter(Document.session_id == session_id)
    return query


def _find_file_duplicate(
    db: Session,
    *,
    user_id: str,
    session_id: str | None,
    scope: str,
    file_name: str,
    file_size: int,
    file_hash: str,
) -> Document | None:
    query = _scoped_documents_query(
        db, user_id=user_id, session_id=session_id, scope=scope
    ).filter(
        Document.file_name == file_name,
        Document.parse_status == "ready",
    )
    exact = query.filter(Document.file_hash == file_hash).order_by(Document.created_at.desc()).first()
    if exact:
        return exact

    # Lazy migration for documents indexed before file_hash existed. Only
    # compare same-name, same-size files and keep the newest valid version.
    legacy_candidates = query.filter(Document.file_hash.is_(None)).order_by(
        Document.created_at.desc()
    ).all()
    for candidate in legacy_candidates:
        if candidate.file_size is not None and int(candidate.file_size) != int(file_size):
            continue
        if not candidate.file_path or not os.path.isfile(candidate.file_path):
            continue
        try:
            candidate_hash = sha256_file(candidate.file_path)
        except OSError:
            continue
        candidate.file_hash = candidate_hash
        if candidate_hash == file_hash:
            db.flush()
            return candidate
    return None


def _remove_file_if_distinct(file_path: str, preserved_path: str | None = None) -> None:
    if not file_path or not os.path.isfile(file_path):
        return
    if preserved_path and os.path.abspath(file_path) == os.path.abspath(preserved_path):
        return
    try:
        os.remove(file_path)
    except OSError as exc:
        logger.warning("Could not remove uploaded duplicate file %s: %s", file_path, exc)


def _delete_superseded_documents(
    db: Session,
    *,
    user_id: str,
    session_id: str | None,
    scope: str,
    file_name: str,
    keep_document_id: str,
) -> int:
    documents = (
        _scoped_documents_query(
            db, user_id=user_id, session_id=session_id, scope=scope
        )
        .filter(
            Document.file_name == file_name,
            Document.parse_status.in_(("ready", "legacy")),
            Document.document_id != keep_document_id,
        )
        .all()
    )
    if not documents:
        return 0

    document_ids = [item.document_id for item in documents]
    file_paths = [item.file_path for item in documents if item.file_path]
    es = _es_client()
    if es.indices.exists(index=settings.rag_index_name):
        es.delete_by_query(
            index=settings.rag_index_name,
            query={"terms": {"document_id": document_ids}},
            refresh=True,
            conflicts="proceed",
        )
    for item in documents:
        db.delete(item)
    db.commit()
    for old_path in file_paths:
        _remove_file_if_distinct(old_path)
    return len(documents)


def _build_chunks(
    blocks: list[ParsedBlock], document: Document, expires_at: datetime | None
) -> list[DocumentChunk]:
    title = Path(document.file_name).stem
    embedding_inputs = [
        "\n".join([title, " > ".join(block.heading_path), block.text]).strip() for block in blocks
    ]
    from service.core.rag.nlp.model import generate_embedding

    embeddings = generate_embedding(
        embedding_inputs,
        model_name=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
    if not embeddings or len(embeddings) != len(blocks) or any(value is None for value in embeddings):
        raise RuntimeError("Embedding generation failed; no partial document was indexed")

    chunks: list[DocumentChunk] = []
    for index, (block, embedding) in enumerate(zip(blocks, embeddings)):
        chunk_hash = sha256_text(block.text)
        chunk_id = hashlib.sha256(
            f"{document.document_id}:{index}:{chunk_hash}".encode("utf-8")
        ).hexdigest()
        chunks.append(
            DocumentChunk(
                chunk_id=chunk_id,
                document_id=document.document_id,
                user_id=document.user_id,
                session_id=document.session_id,
                scope=document.scope,
                document_name=document.file_name,
                content=block.text,
                chunk_hash=chunk_hash,
                chunk_index=index,
                block_type=block.block_type,
                heading_path=block.heading_path,
                page=block.page,
                positions=block.positions,
                title_tokens=tokenize_text(title),
                heading_tokens=tokenize_text(" ".join(block.heading_path)),
                content_tokens=tokenize_text(normalize_text(block.text)),
                embedding=list(embedding),
                expires_at=expires_at.isoformat() if expires_at else None,
            )
        )
    return chunks


def _index_chunks(chunks: list[DocumentChunk]) -> None:
    ensure_rag_index()
    actions = [
        {
            "_op_type": "index",
            "_index": settings.rag_index_name,
            "_id": item.chunk_id,
            "_source": item.to_index_document(),
        }
        for item in chunks
    ]
    bulk(_es_client(), actions, refresh="wait_for", request_timeout=120)


def cleanup_expired_documents(db: Session | None = None) -> int:
    owns_session = db is None
    if db is None:
        from utils.database import SessionLocal

        db = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        expired = db.query(Document).filter(Document.expires_at.isnot(None), Document.expires_at < now).all()
        if not expired:
            return 0
        ids = [item.document_id for item in expired]
        paths = [item.file_path for item in expired if item.file_path]
        es = _es_client()
        if es.indices.exists(index=settings.rag_index_name):
            es.delete_by_query(
                index=settings.rag_index_name,
                query={"terms": {"document_id": ids}},
                refresh=True,
                conflicts="proceed",
            )
        for item in expired:
            db.delete(item)
        db.commit()
        for file_path in paths:
            if os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                except OSError as exc:
                    logger.warning("Could not remove expired document file %s: %s", file_path, exc)
        return len(ids)
    finally:
        if owns_session:
            db.close()


def ingest_document(
    *,
    file_path: str,
    file_name: str,
    user_id: str,
    session_id: str | None = None,
    scope: str = "user",
    mime_type: str | None = None,
    file_size: int | None = None,
    db: Session | None = None,
) -> dict:
    if scope not in {"user", "session"}:
        raise ValueError("scope must be 'user' or 'session'")
    if scope == "session" and not session_id:
        raise ValueError("session_id is required for session-scoped documents")

    owns_session = db is None
    if db is None:
        from utils.database import SessionLocal

        db = SessionLocal()
    try:
        cleanup_expired_documents(db)
    except Exception as exc:
        # Expired session documents are already excluded by retrieval filters;
        # cleanup failure must not block a new upload.
        logger.warning("Lazy expired-document cleanup failed: %s", exc)
    actual_file_size = file_size if file_size is not None else os.path.getsize(file_path)
    file_hash = sha256_file(file_path)
    file_duplicate = _find_file_duplicate(
        db,
        user_id=user_id,
        session_id=session_id,
        scope=scope,
        file_name=file_name,
        file_size=actual_file_size,
        file_hash=file_hash,
    )
    if file_duplicate:
        file_duplicate.file_hash = file_hash
        file_duplicate.updated_at = datetime.now(timezone.utc)
        db.commit()
        replaced_count = 0
        try:
            replaced_count = _delete_superseded_documents(
                db,
                user_id=user_id,
                session_id=session_id,
                scope=scope,
                file_name=file_name,
                keep_document_id=file_duplicate.document_id,
            )
        except Exception as exc:
            db.rollback()
            logger.warning("Could not remove superseded exact duplicates: %s", exc)
        _remove_file_if_distinct(file_path, file_duplicate.file_path)
        return {
            "document_id": file_duplicate.document_id,
            "deduplicated": True,
            "replaced_count": replaced_count,
            "chunk_count": file_duplicate.chunk_count,
            "parse_status": file_duplicate.parse_status,
        }

    expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=settings.temp_document_ttl_hours)
        if scope == "session"
        else None
    )
    document = Document(
        user_id=user_id,
        session_id=session_id if scope == "session" else None,
        scope=scope,
        file_name=file_name,
        file_path=file_path,
        mime_type=mime_type or mimetypes.guess_type(file_name)[0],
        file_size=actual_file_size,
        file_hash=file_hash,
        parse_status="pending",
        expires_at=expires_at,
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    try:
        parsed_blocks = parse_blocks(file_path)
        blocks = structure_aware_chunks(parsed_blocks)
        if not blocks:
            raise RuntimeError("The parser returned no usable content")
        content_hash = sha256_text("\n".join(block.text for block in parsed_blocks))
        duplicate = _find_duplicate(
            db,
            user_id=user_id,
            session_id=session_id,
            scope=scope,
            content_hash=content_hash,
        )
        if duplicate and duplicate.document_id != document.document_id:
            duplicate.file_hash = duplicate.file_hash or file_hash
            duplicate.updated_at = datetime.now(timezone.utc)
            db.delete(document)
            db.commit()
            replaced_count = 0
            try:
                replaced_count = _delete_superseded_documents(
                    db,
                    user_id=user_id,
                    session_id=session_id,
                    scope=scope,
                    file_name=file_name,
                    keep_document_id=duplicate.document_id,
                )
            except Exception as exc:
                db.rollback()
                logger.warning("Could not remove superseded parsed duplicates: %s", exc)
            _remove_file_if_distinct(file_path, duplicate.file_path)
            return {
                "document_id": duplicate.document_id,
                "deduplicated": True,
                "replaced_count": replaced_count,
                "chunk_count": duplicate.chunk_count,
                "parse_status": duplicate.parse_status,
            }

        document.content_hash = content_hash
        chunks = _build_chunks(blocks, document, expires_at)
        _index_chunks(chunks)
        document.parse_status = "ready"
        document.chunk_count = len(chunks)
        document.error_message = None
        db.commit()
        replaced_count = 0
        try:
            replaced_count = _delete_superseded_documents(
                db,
                user_id=user_id,
                session_id=session_id,
                scope=scope,
                file_name=file_name,
                keep_document_id=document.document_id,
            )
        except Exception as exc:
            db.rollback()
            logger.warning("Could not remove superseded document versions: %s", exc)
        return {
            "document_id": document.document_id,
            "deduplicated": False,
            "replaced_count": replaced_count,
            "chunk_count": document.chunk_count,
            "parse_status": document.parse_status,
        }
    except Exception as exc:
        db.rollback()
        try:
            es = _es_client()
            if es.indices.exists(index=settings.rag_index_name):
                es.delete_by_query(
                    index=settings.rag_index_name,
                    query={"term": {"document_id": document.document_id}},
                    refresh=True,
                    conflicts="proceed",
                )
        except Exception:
            logger.warning("Could not clean partial ES chunks for document_id=%s", document.document_id)
        failed = db.query(Document).filter(Document.document_id == document.document_id).first()
        if failed:
            failed.parse_status = "failed"
            failed.error_message = str(exc)[:2000]
            db.commit()
        logger.exception("Document ingestion failed for document_id=%s", document.document_id)
        raise
    finally:
        if owns_session:
            db.close()


def get_session_document_content(user_id: str, session_id: str) -> dict:
    """Compatibility view for the existing get_parsed_content endpoint."""

    es = _es_client()
    if not es.indices.exists(index=settings.rag_index_name):
        raise LookupError("No parsed document was found for this session")
    response = es.search(
        index=settings.rag_index_name,
        body={
            "size": 1000,
            "_source": ["content", "document_name", "document_id", "expires_at"],
            "sort": [{"document_id": "asc"}, {"chunk_index": "asc"}],
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"user_id": user_id}},
                        {"term": {"scope": "session"}},
                        {"term": {"session_id": session_id}},
                        {"range": {"expires_at": {"gt": "now"}}},
                    ]
                }
            },
        },
    )
    hits = response.get("hits", {}).get("hits", [])
    if not hits:
        raise LookupError("No parsed document was found for this session")
    content = "\n\n".join(str(hit["_source"].get("content") or "") for hit in hits)
    first = hits[0]["_source"]
    return {
        "status": "success",
        "session_id": session_id,
        "document_id": first.get("document_id"),
        "filename": first.get("document_name"),
        "content": content,
        "content_length": len(content),
        "expires_at": first.get("expires_at"),
    }
