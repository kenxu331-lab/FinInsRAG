"""Small SSE orchestration layer for grounded document chat."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

from openai import OpenAI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from config import settings
from service.core.citation_feedback import (
    clear_citation_warning,
    has_citation_warning,
    mark_citation_warning,
)
from service.core.generation import (
    CITATION_PATTERN,
    NO_ANSWER,
    NO_REASONING_ANSWER,
    deepseek_thinking_body,
    stream_recovered_answer_from_reasoning,
    stream_grounded_draft,
    validate_citation_syntax,
)
from service.core.ingestion import count_tokens
from service.core.types import Evidence
from utils import logger
from utils.database import SessionLocal


def get_chat_client() -> OpenAI:
    """Return the configured DeepSeek-compatible client."""

    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    return OpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
    )


def _extract_json_object(raw: str) -> dict[str, Any]:
    value = (raw or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}


_LEGACY_CITATION_PATTERN = re.compile(r"##\d+\$\$")


def _strip_history_citations(value: str) -> str:
    value = CITATION_PATTERN.sub("", value or "")
    value = _LEGACY_CITATION_PATTERN.sub("", value)
    return re.sub(r"[ \t]+\n", "\n", value).strip()


def _truncate_to_tokens(value: str, token_budget: int) -> str:
    """Keep a deterministic prefix within the approximate token budget."""

    if token_budget <= 0 or not value:
        return ""
    if count_tokens(value) <= token_budget:
        return value
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if count_tokens(value[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return value[:low].rstrip()


def _history_row_value(row: Any, name: str) -> str:
    if hasattr(row, name):
        return str(getattr(row, name) or "")
    mapping = getattr(row, "_mapping", {})
    return str(mapping.get(name) or "")


def _fit_history_window(
    rows_newest_first: Iterable[Any],
    max_turns: int,
    token_budget: int,
) -> list[dict[str, str]]:
    """Select recent complete turns and return them in chronological order."""

    if max_turns <= 0 or token_budget <= 0:
        return []
    selected: list[dict[str, str]] = []
    remaining = token_budget
    for row in list(rows_newest_first)[:max_turns]:
        question = _strip_history_citations(_history_row_value(row, "user_question"))
        answer = _strip_history_citations(_history_row_value(row, "model_answer"))
        if not question:
            continue
        turn_tokens = count_tokens(question) + count_tokens(answer) + 8
        if turn_tokens > remaining:
            if selected or remaining <= 8:
                break
            question_budget = min(count_tokens(question), max(1, remaining // 3))
            question = _truncate_to_tokens(question, question_budget)
            answer_budget = max(0, remaining - count_tokens(question) - 8)
            answer = _truncate_to_tokens(answer, answer_budget)
            turn_tokens = count_tokens(question) + count_tokens(answer) + 8
        if turn_tokens > remaining or not question:
            break
        selected.append({"user_question": question, "model_answer": answer})
        remaining -= turn_tokens
    selected.reverse()
    return selected


def load_conversation_history(session_id: str, user_id: str) -> list[dict[str, str]]:
    """Load an isolated, bounded history window from PostgreSQL."""

    if settings.history_turns <= 0 or settings.history_token_budget <= 0:
        return []
    with SessionLocal() as db:
        rows = db.execute(
            text(
                """
                SELECT m.user_question, m.model_answer
                FROM messages AS m
                JOIN sessions AS s ON s.session_id = m.session_id
                WHERE m.session_id = :session_id AND s.user_id = :user_id
                ORDER BY m.created_at DESC, m.message_id DESC
                LIMIT :history_turns
                """
            ),
            {
                "session_id": session_id,
                "user_id": user_id,
                "history_turns": settings.history_turns,
            },
        ).fetchall()
    return _fit_history_window(rows, settings.history_turns, settings.history_token_budget)


def rewrite_followup_question(question: str, history: list[dict[str, str]]) -> str:
    """Rewrite a follow-up into a standalone retrieval query without answering it."""

    question = question.strip()
    if not history:
        return question
    prompt = f"""
把当前追问改写成可独立用于文档检索的问题。只补全代词、省略的对象、比较项和问题维度，不回答问题，不添加历史中没有的主题。
历史回答仅用于理解对话指代，不代表事实证据。只返回 JSON：{{"standalone_query": "..."}}。

历史对话：
{json.dumps(history, ensure_ascii=False)}

当前追问：{question}
""".strip()
    try:
        completion = get_chat_client().chat.completions.create(
            model=settings.chat_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            stream=False,
            temperature=0,
            timeout=30,
            extra_body=deepseek_thinking_body(False),
        )
        payload = _extract_json_object(completion.choices[0].message.content or "")
        standalone = _strip_history_citations(str(payload.get("standalone_query") or ""))
        return standalone[:1000] if standalone else question
    except Exception as exc:
        logger.warning("Follow-up query rewrite failed: %s", exc)
        previous_question = history[-1].get("user_question", "").strip()
        return f"{previous_question}；{question}"[:1000] if previous_question else question


def generate_recommended_questions(
    user_question: str,
    retrieved_content: list[dict[str, Any]] | None = None,
    session_id: str | None = None,
) -> list[str]:
    """Generate up to three optional follow-up questions."""

    del session_id  # Kept in the signature for API compatibility.
    document_names = sorted(
        {
            str(item.get("document_name"))
            for item in (retrieved_content or [])
            if item.get("document_name")
        }
    )[:3]
    context_hint = "、".join(document_names) if document_names else "无"
    prompt = f"""
根据用户问题生成最多 3 个简短、具体、互不重复的后续问题。
只返回 JSON：{{"recommended_questions": ["问题1", "问题2", "问题3"]}}。
不得声称文档中存在未提供的信息。

用户问题：{user_question}
相关文档名称：{context_hint}
""".strip()
    try:
        completion = get_chat_client().chat.completions.create(
            model=settings.chat_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            stream=False,
            temperature=settings.generation_temperature,
            timeout=30,
            extra_body=deepseek_thinking_body(False),
        )
        payload = _extract_json_object(completion.choices[0].message.content or "")
        questions = payload.get("recommended_questions")
        if not isinstance(questions, list):
            return []
        return [str(item).strip() for item in questions if str(item).strip()][:3]
    except Exception as exc:
        logger.warning("Recommended question generation failed: %s", exc)
        return []


def generate_session_name(user_question: str) -> str:
    """Generate a compact session title and fall back deterministically."""

    fallback = user_question.strip()[:80] or "文档问答"
    prompt = f"""
为下面的问题生成不超过 20 个汉字的会话标题。
只返回 JSON：{{"session_name": "标题"}}。

问题：{user_question}
""".strip()
    try:
        completion = get_chat_client().chat.completions.create(
            model=settings.chat_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            stream=False,
            temperature=settings.generation_temperature,
            timeout=30,
            extra_body=deepseek_thinking_body(False),
        )
        payload = _extract_json_object(completion.choices[0].message.content or "")
        name = str(payload.get("session_name") or "").strip()
        return name[:255] if name else fallback
    except Exception as exc:
        logger.warning("Session name generation failed: %s", exc)
        return fallback


def update_session_name(session_id: str, question: str, user_id: str) -> None:
    """Create the session row once without changing an existing owner's row."""

    with SessionLocal() as db:
        try:
            existing = db.execute(
                text("SELECT user_id FROM sessions WHERE session_id = :session_id"),
                {"session_id": session_id},
            ).fetchone()
            if existing:
                if str(existing.user_id) != str(user_id):
                    raise PermissionError("Session belongs to another user")
                return
            db.execute(
                text(
                    """
                    INSERT INTO sessions (session_id, user_id, session_name)
                    VALUES (:session_id, :user_id, :session_name)
                    """
                ),
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "session_name": generate_session_name(question),
                },
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


def write_chat_to_db(
    session_id: str,
    user_question: str,
    model_answer: str,
    evidence: list[dict[str, Any]],
    recommended_questions: list[str],
    think: str = "",
) -> None:
    """Persist the answer and only the evidence actually cited by it."""

    with SessionLocal() as db:
        try:
            db.execute(
                text(
                    """
                    INSERT INTO messages
                        (session_id, user_question, model_answer, documents,
                         recommended_questions, think)
                    VALUES
                        (:session_id, :user_question, :model_answer, :documents,
                         :recommended_questions, :think)
                    """
                ),
                {
                    "session_id": session_id,
                    "user_question": user_question,
                    "model_answer": model_answer,
                    "documents": json.dumps(evidence, ensure_ascii=False),
                    "recommended_questions": json.dumps(
                        recommended_questions, ensure_ascii=False
                    ),
                    "think": think[: settings.reasoning_display_max_chars],
                },
            )
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise


def _as_evidence(retrieved_content: Iterable[dict[str, Any]]) -> list[Evidence]:
    result: list[Evidence] = []
    for index, item in enumerate(retrieved_content, start=1):
        result.append(
            Evidence(
                citation_id=str(item.get("citation_id") or item.get("id") or f"C{index}"),
                chunk_id=str(item.get("chunk_id") or item.get("id") or ""),
                document_id=str(item.get("document_id") or ""),
                document_name=str(item.get("document_name") or ""),
                content=str(item.get("content_with_weight") or item.get("content") or ""),
                page=item.get("page"),
                heading_path=[str(value) for value in (item.get("heading_path") or [])],
                block_type=str(item.get("block_type") or "paragraph"),
                positions=item.get("positions") or [],
                retrieval_sources=item.get("retrieval_sources") or [],
            )
        )
    return result


def _sse(payload: dict[str, Any]) -> str:
    return "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def get_chat_completion(
    session_id: str,
    question: str,
    retrieved_content: list[dict[str, Any]] | None,
    user_id: str,
    request_id: str = "",
    history: list[dict[str, str]] | None = None,
):
    """Stream evidence, reasoning and final content directly over SSE."""

    documents = list(retrieved_content or [])
    yield _sse({"documents": documents})

    try:
        generation_started = time.perf_counter()
        evidence = _as_evidence(documents)
        raw_reasoning_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_chars = 0
        draft_parts: list[str] = []
        reasoning_only_recovery = False
        citation_reminder = has_citation_warning(user_id, session_id)
        if evidence:
            for kind, delta in stream_grounded_draft(
                question, evidence, history, citation_reminder
            ):
                if kind == "content":
                    draft_parts.append(delta)
                    yield _sse(
                        {
                            "role": "assistant",
                            "content": delta,
                            "thinking": False,
                        }
                    )
                    continue
                raw_reasoning_parts.append(delta)
                remaining = settings.reasoning_display_max_chars - reasoning_chars
                if remaining <= 0:
                    continue
                displayed = delta[:remaining]
                reasoning_parts.append(displayed)
                reasoning_chars += len(displayed)
                yield _sse(
                    {
                        "role": "assistant",
                        "content": displayed,
                        "thinking": True,
                    }
                )
        answer = "".join(draft_parts) if evidence else NO_ANSWER
        if not evidence:
            yield _sse(
                {
                    "role": "assistant",
                    "content": answer,
                    "thinking": False,
                }
            )
        if evidence and not answer.strip() and raw_reasoning_parts:
            logger.info(
                "request_id=%s reasoning_only_response=true reasoning_chars=%s",
                request_id or "-",
                sum(len(part) for part in raw_reasoning_parts),
            )
            recovered_parts: list[str] = []
            for delta in stream_recovered_answer_from_reasoning(
                "".join(raw_reasoning_parts)
            ):
                recovered_parts.append(delta)
                yield _sse(
                    {
                        "role": "assistant",
                        "content": delta,
                        "thinking": False,
                    }
                )
            answer = "".join(recovered_parts)
            reasoning_only_recovery = True
        if evidence and not answer.strip():
            raise RuntimeError("model returned no final answer content")
        if reasoning_only_recovery:
            if answer.strip() == NO_REASONING_ANSWER:
                raise RuntimeError("no explicit answer found in reasoning content")
        citation_valid = validate_citation_syntax(answer, evidence)
        if not citation_valid:
            logger.warning(
                "request_id=%s citation_validation=false answer_chars=%s",
                request_id or "-",
                len(answer),
            )
            if evidence:
                mark_citation_warning(user_id, session_id)
        elif evidence and citation_reminder:
            clear_citation_warning(user_id, session_id)
        generation_ms = round((time.perf_counter() - generation_started) * 1000, 1)

        recommended_questions = generate_recommended_questions(question, documents, session_id)
        if recommended_questions:
            yield _sse({"recommended_questions": recommended_questions})

        cited = {f"C{number}" for number in CITATION_PATTERN.findall(answer)}
        used_evidence = [
            item
            for item in documents
            if str(item.get("citation_id") or item.get("id") or "") in cited
        ]
        logger.info(
            "request_id=%s generation_ms=%s context_tokens=%s refusal_reason=%s citations=%s",
            request_id or "-",
            generation_ms,
            sum(
                count_tokens(str(item.get("content_with_weight") or item.get("content") or ""))
                for item in documents
            ),
            "insufficient_evidence" if answer == NO_ANSWER else "none",
            len(used_evidence),
        )
        update_session_name(session_id, question, user_id)
        write_chat_to_db(
            session_id,
            question,
            answer,
            used_evidence,
            recommended_questions,
            "".join(reasoning_parts),
        )
        yield "event: end\ndata: [DONE]\n\n"
    except Exception as exc:
        logger.exception("request_id=%s grounded chat failed: %s", request_id or "-", exc)
        yield "event: error\ndata: " + json.dumps(
            {"role": "error", "content": "回答生成失败，请稍后重试"},
            ensure_ascii=False,
        ) + "\n\n"
