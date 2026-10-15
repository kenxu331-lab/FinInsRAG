"""Run the 40-case retrieval and grounded-answer evaluation set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.core.context import pack_context  # noqa: E402
from service.core.generation import NO_ANSWER, generate_grounded_answer, validate_citation_syntax  # noqa: E402
from service.core.ingestion import ingest_document  # noqa: E402
from service.core.retrieval import hybrid_retrieve  # noqa: E402
from utils.database import SessionLocal  # noqa: E402


def _load(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _ingest_fixtures(user_id: str) -> None:
    db = SessionLocal()
    try:
        for source in sorted((Path(__file__).parent / "fixtures").glob("*.txt")):
            ingest_document(
                file_path=str(source),
                file_name=source.name,
                user_id=user_id,
                scope="user",
                file_size=source.stat().st_size,
                db=db,
            )
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", default="rag-eval-user")
    parser.add_argument("--session-id")
    parser.add_argument("--ingest-fixtures", action="store_true")
    parser.add_argument("--with-generation", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).parent / "rag_eval_40.jsonl")
    args = parser.parse_args()

    if args.ingest_fixtures:
        _ingest_fixtures(args.user_id)

    cases = _load(args.dataset)
    retrieval_hits = 0
    reciprocal_ranks = []
    answer_hits = 0
    citation_hits = 0
    details = []

    for case in cases:
        candidates = hybrid_retrieve(args.user_id, args.session_id, case["question"])
        documents = [str(item["source"].get("document_name") or "") for item in candidates]
        expected = set(case["expected_documents"])
        retrieved = set(documents[:20])
        retrieval_ok = not expected or expected.issubset(retrieved)
        retrieval_hits += int(retrieval_ok)

        ranks = [documents.index(name) + 1 for name in expected if name in documents[:10]]
        reciprocal_ranks.append(1 / min(ranks) if ranks else (1.0 if not expected else 0.0))
        row = {"id": case["id"], "retrieval_ok": retrieval_ok, "documents": documents[:5]}

        if args.with_generation:
            evidence = pack_context(candidates)
            answer = generate_grounded_answer(case["question"], evidence)
            if case["answerable"]:
                answer_ok = all(value in answer for value in case["expected_answer_contains"])
                citation_ok = validate_citation_syntax(answer, evidence)
            else:
                answer_ok = answer == NO_ANSWER
                citation_ok = True
            answer_hits += int(answer_ok)
            citation_hits += int(citation_ok)
            row.update({"answer_ok": answer_ok, "citation_ok": citation_ok, "answer": answer})
        details.append(row)

    report = {
        "cases": len(cases),
        "recall_at_20": retrieval_hits / len(cases),
        "mrr_at_10": sum(reciprocal_ranks) / len(reciprocal_ranks),
        "answer_accuracy": answer_hits / len(cases) if args.with_generation else None,
        "citation_validity": citation_hits / len(cases) if args.with_generation else None,
        "details": [] if args.summary_only else details,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["recall_at_20"] >= 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
