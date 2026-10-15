"""Canonical document deletion for PostgreSQL, Elasticsearch and local storage."""

from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

from config import settings
from models.document import Document
from service.core.rag.utils.es_conn import ESConnection

logger = logging.getLogger(__name__)


def delete_document(user_id: str, file_name: str, db: Session) -> dict:
    documents = (
        db.query(Document)
        .filter(
            Document.user_id == user_id,
            Document.file_name == file_name,
            Document.scope == "user",
        )
        .order_by(Document.created_at.desc())
        .all()
    )
    if not documents:
        return {"status": "error", "message": "Document not found"}

    try:
        deleted_chunks = 0
        document_ids = [item.document_id for item in documents]
        es = ESConnection().es
        if es.indices.exists(index=settings.rag_index_name):
            response = es.delete_by_query(
                index=settings.rag_index_name,
                query={
                    "bool": {
                        "filter": [
                            {"term": {"user_id": user_id}},
                            {"terms": {"document_id": document_ids}},
                        ]
                    }
                },
                refresh=True,
                conflicts="proceed",
            )
            deleted_chunks = int(response.get("deleted") or 0)

        file_paths = [item.file_path for item in documents if item.file_path]
        for document in documents:
            db.delete(document)
        db.commit()
        for file_path in file_paths:
            if not os.path.isfile(file_path):
                continue
            try:
                os.remove(file_path)
            except OSError as exc:
                logger.warning("Could not remove document file %s: %s", file_path, exc)
        return {
            "status": "success",
            "message": "Document deleted",
            "document_id": document_ids[0],
            "deleted_documents": len(document_ids),
            "deleted_chunks": deleted_chunks,
        }
    except Exception as exc:
        db.rollback()
        logger.exception("Document deletion failed")
        return {"status": "error", "message": f"Failed to delete document: {exc}"}
