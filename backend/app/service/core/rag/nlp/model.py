"""Embedding adapter retained under the original import path."""

from __future__ import annotations

import logging
from typing import List

from openai import OpenAI

from config import settings

logger = logging.getLogger(__name__)


def generate_embedding(
    text: str | List[str],
    api_key: str | None = None,
    base_url: str | None = None,
    model_name: str | None = None,
    dimensions: int | None = None,
    encoding_format: str = "float",
    max_batch_size: int = 10,
):
    client = OpenAI(
        api_key=api_key or settings.dashscope_api_key,
        base_url=base_url or settings.dashscope_base_url,
    )
    model = model_name or settings.embedding_model
    dims = dimensions or settings.embedding_dimensions

    if isinstance(text, str):
        try:
            response = client.embeddings.create(
                model=model,
                input=text,
                dimensions=dims,
                encoding_format=encoding_format,
            )
            return response.data[0].embedding
        except Exception as exc:
            logger.warning("Query embedding request failed: %s", exc)
            return None

    embeddings: list[list[float] | None] = []
    for offset in range(0, len(text), max_batch_size):
        batch = text[offset : offset + max_batch_size]
        try:
            response = client.embeddings.create(
                model=model,
                input=batch,
                dimensions=dims,
                encoding_format=encoding_format,
            )
            embeddings.extend(item.embedding for item in response.data)
        except Exception as exc:
            logger.warning("Batch embedding request failed at offset=%s: %s", offset, exc)
            embeddings.extend([None] * len(batch))
    return embeddings


def rerank_similarity(*_args, **_kwargs):
    raise RuntimeError("The legacy weighted reranker was removed; use independent recall plus RRF")
