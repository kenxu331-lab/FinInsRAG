"""Small tokenizer boundary used by indexing and BM25 queries."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def tokenize_text(text: str) -> str:
    try:
        from service.core.rag.nlp import rag_tokenizer

        return rag_tokenizer.tokenize(text or "")
    except ImportError as exc:
        # Keeps diagnostics and lightweight unit tests usable when the optional
        # native trie dependency is absent. The production image installs it.
        logger.warning("RAG tokenizer unavailable, using basic tokenizer: %s", exc)
        tokens = re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+", (text or "").lower())
        return " ".join(tokens)
