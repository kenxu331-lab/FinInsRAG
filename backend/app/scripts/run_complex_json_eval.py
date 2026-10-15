"""Run an independent, one-pass RAG evaluation for JSON-array gold cases."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.core.context import pack_context
from service.core.generation import (
    CITATION_PATTERN,
    NO_ANSWER,
    recover_answer_from_reasoning,
    stream_grounded_draft,
    validate_citation_syntax,
)
from service.core.retrieval import hybrid_retrieve


def initial_result(index: int, case: dict) -> dict:
    return {
        "index": index + 1,
        **case,
        "system_answer": "",
        "retrieval_count": 0,
        "context_count": 0,
        "retrieved_documents": [],
        "retrieved_chunk_ids": [],
        "used_citations": [],
        "citation_valid": False,
        "refused": False,
        "reasoning_chars": 0,
        "elapsed_seconds": 0.0,
        "error": None,
    }


def run_case(index: int, case: dict, user_id: str, session_id: str | None) -> dict:
    started = time.perf_counter()
    question = str(case["question"])
    result = initial_result(index, case)
    try:
        candidates = hybrid_retrieve(user_id, session_id, question)
        evidence = pack_context(candidates)
        result["retrieval_count"] = len(candidates)
        result["context_count"] = len(evidence)
        result["retrieved_documents"] = sorted(
            {
                str(item.get("source", {}).get("document_name") or "")
                for item in candidates
                if item.get("source", {}).get("document_name")
            }
        )
        result["retrieved_chunk_ids"] = [item.chunk_id for item in evidence]

        if not evidence:
            answer = NO_ANSWER
            reasoning = ""
        else:
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            for kind, delta in stream_grounded_draft(question, evidence, []):
                if kind == "content":
                    content_parts.append(delta)
                else:
                    reasoning_parts.append(delta)
            answer = "".join(content_parts).strip()
            reasoning = "".join(reasoning_parts)
            if not answer and reasoning:
                answer = recover_answer_from_reasoning(reasoning)
            if not answer:
                raise RuntimeError("model returned no final answer content")

        result["system_answer"] = answer
        result["used_citations"] = sorted(
            {f"C{number}" for number in CITATION_PATTERN.findall(answer)},
            key=lambda value: int(value[1:]),
        )
        result["citation_valid"] = validate_citation_syntax(answer, evidence)
        result["refused"] = answer.strip() == NO_ANSWER
        result["reasoning_chars"] = len(reasoning)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--case-timeout", type=int, default=60)
    parser.add_argument("--skip-indices", default="")
    args = parser.parse_args()

    cases = json.loads(args.dataset.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("dataset must be a JSON array")

    skip_indices = {
        int(value)
        for value in args.skip_indices.split(",")
        if value.strip().isdigit()
    }
    results: list[dict | None] = [None] * len(cases)

    def report(index: int, row: dict) -> None:
        status = "error" if row["error"] else ("refused" if row["refused"] else "answered")
        print(
            f"[{index + 1:02d}/{len(cases)}] {status} "
            f"retrieval={row['retrieval_count']} context={row['context_count']} "
            f"citations={len(row['used_citations'])} elapsed={row['elapsed_seconds']}s",
            flush=True,
        )

    if max(1, args.workers) == 1:
        def timeout_handler(_signum, _frame):
            raise TimeoutError(f"case exceeded {args.case_timeout} seconds")

        signal.signal(signal.SIGALRM, timeout_handler)
        for index, case in enumerate(cases):
            if index + 1 in skip_indices:
                row = initial_result(index, case)
                row["error"] = (
                    "TimeoutError: first attempt exceeded 180 seconds and was "
                    "terminated; not retried"
                )
            else:
                try:
                    signal.setitimer(signal.ITIMER_REAL, max(1, args.case_timeout))
                    row = run_case(index, case, args.user_id, args.session_id)
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
            results[index] = row
            report(index, row)
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(run_case, index, case, args.user_id, args.session_id): index
                for index, case in enumerate(cases)
                if index + 1 not in skip_indices
            }
            for index, case in enumerate(cases):
                if index + 1 in skip_indices:
                    row = initial_result(index, case)
                    row["error"] = "Skipped by --skip-indices"
                    results[index] = row
                    report(index, row)
            for future in as_completed(futures):
                index = futures[future]
                row = future.result()
                results[index] = row
                report(index, row)

    complete = [row for row in results if row is not None]
    payload = {
        "metadata": {
            "dataset": str(args.dataset),
            "user_id": args.user_id,
            "session_id": args.session_id,
            "workers": max(1, args.workers),
            "case_timeout_seconds": args.case_timeout,
            "cases": len(complete),
            "errors": sum(bool(row["error"]) for row in complete),
            "refusals": sum(bool(row["refused"]) for row in complete),
            "citation_valid": sum(bool(row["citation_valid"]) for row in complete),
            "elapsed_seconds_sum": round(
                sum(float(row["elapsed_seconds"]) for row in complete), 3
            ),
        },
        "results": complete,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload["metadata"], ensure_ascii=False), flush=True)
    return 1 if payload["metadata"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
