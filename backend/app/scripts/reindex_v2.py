"""Rebuild legacy knowledge-base files into rag_chunks_v2.

Usage from backend/app:
    python scripts/reindex_v2.py --storage-root service/core/storage/file --dry-run
    python scripts/reindex_v2.py --storage-root service/core/storage/file
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.core.ingestion import ingest_document  # noqa: E402
from utils.database import SessionLocal  # noqa: E402


def _find_source(root: Path, file_name: str) -> Path | None:
    matches = [path for path in root.rglob(file_name) if path.is_file()]
    return sorted(matches, key=lambda value: str(value))[0] if matches else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--user-id", help="Only rebuild one user's documents")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db = SessionLocal()
    report = {"total": 0, "indexed": 0, "deduplicated": 0, "missing": 0, "failed": 0}
    try:
        sql = "SELECT user_id, file_name FROM knowledgebases"
        params = {}
        if args.user_id:
            sql += " WHERE user_id = :user_id"
            params["user_id"] = args.user_id
        rows = db.execute(text(sql), params).fetchall()
        report["total"] = len(rows)
        for row in rows:
            source = _find_source(args.storage_root, row.file_name)
            if not source:
                report["missing"] += 1
                logging.warning("missing user_id=%s file=%s", row.user_id, row.file_name)
                continue
            if args.dry_run:
                logging.info("would index user_id=%s source=%s", row.user_id, source)
                continue
            try:
                result = ingest_document(
                    file_path=str(source),
                    file_name=row.file_name,
                    user_id=str(row.user_id),
                    scope="user",
                    file_size=source.stat().st_size,
                    db=db,
                )
                key = "deduplicated" if result["deduplicated"] else "indexed"
                report[key] += 1
            except Exception:
                report["failed"] += 1
                logging.exception("failed user_id=%s file=%s", row.user_id, row.file_name)
        logging.info("reindex report=%s", report)
        return 1 if report["failed"] else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
