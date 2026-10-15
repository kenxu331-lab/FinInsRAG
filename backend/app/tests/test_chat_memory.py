from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from service.core.chat import (
    _fit_history_window,
    get_chat_completion,
    load_conversation_history,
    rewrite_followup_question,
    write_chat_to_db,
)
from service.core.generation import recover_answer_from_reasoning, stream_grounded_draft
from service.core.ingestion import count_tokens
from service.core.types import Evidence


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeDatabase:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return FakeResult(self.rows)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


class HistoryWindowTests(unittest.TestCase):
    def test_history_uses_current_user_session_and_last_four_turns(self):
        rows = [
            SimpleNamespace(user_question=f"问题{i}", model_answer=f"回答{i}。[C{i}]")
            for i in range(6, 0, -1)
        ]
        fake_db = FakeDatabase(rows)
        with patch("service.core.chat.SessionLocal", return_value=fake_db):
            history = load_conversation_history("session-a", "user-a")

        self.assertEqual([turn["user_question"] for turn in history], ["问题3", "问题4", "问题5", "问题6"])
        self.assertTrue(all("[C" not in turn["model_answer"] for turn in history))
        statement, params = fake_db.calls[0]
        self.assertIn("JOIN sessions", statement)
        self.assertIn("s.user_id = :user_id", statement)
        self.assertEqual(params["session_id"], "session-a")
        self.assertEqual(params["user_id"], "user-a")
        self.assertEqual(params["history_turns"], 4)

    def test_history_never_exceeds_token_budget(self):
        rows = [SimpleNamespace(user_question="上一问", model_answer="很长的回答" * 2000)]
        history = _fit_history_window(rows, max_turns=4, token_budget=2000)
        used = sum(
            count_tokens(turn["user_question"]) + count_tokens(turn["model_answer"]) + 8
            for turn in history
        )
        self.assertLessEqual(used, 2000)
        self.assertEqual(len(history), 1)

    def test_followup_is_rewritten_without_historical_reasoning(self):
        calls = []

        class Completions:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                message = SimpleNamespace(
                    content=json.dumps(
                        {"standalone_query": "意外医疗是否有等待期限制？"},
                        ensure_ascii=False,
                    )
                )
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        history = [
            {
                "user_question": "疾病医疗等待期多久？",
                "model_answer": "疾病医疗有等待期。",
            }
        ]
        with patch("service.core.chat.get_chat_client", return_value=client):
            rewritten = rewrite_followup_question("那意外医疗呢？", history)

        self.assertEqual(rewritten, "意外医疗是否有等待期限制？")
        self.assertEqual(calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        prompt = calls[0]["messages"][0]["content"]
        self.assertIn("疾病医疗等待期多久", prompt)
        self.assertNotIn("reasoning_content", prompt)


class ThinkingStreamTests(unittest.TestCase):
    def setUp(self):
        self.has_warning_patcher = patch(
            "service.core.chat.has_citation_warning", return_value=False
        )
        self.mark_warning_patcher = patch("service.core.chat.mark_citation_warning")
        self.clear_warning_patcher = patch("service.core.chat.clear_citation_warning")
        self.has_warning = self.has_warning_patcher.start()
        self.mark_warning = self.mark_warning_patcher.start()
        self.clear_warning = self.clear_warning_patcher.start()
        self.addCleanup(self.has_warning_patcher.stop)
        self.addCleanup(self.mark_warning_patcher.stop)
        self.addCleanup(self.clear_warning_patcher.stop)

    def test_native_reasoning_and_answer_deltas_are_separated(self):
        calls = []

        class Completions:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                return iter(
                    [
                        SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    delta=SimpleNamespace(reasoning_content="分析证据", content=None)
                                )
                            ]
                        ),
                        SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    delta=SimpleNamespace(reasoning_content=None, content="答案[C1]")
                                )
                            ]
                        ),
                    ]
                )

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        evidence = [Evidence("C1", "chunk-1", "doc-1", "条款", "意外医疗不受等待期限制。")]
        with patch("service.core.generation._client", return_value=client):
            deltas = list(stream_grounded_draft("那意外医疗呢？", evidence, []))

        self.assertEqual(deltas, [("reasoning", "分析证据"), ("content", "答案[C1]")])
        self.assertEqual(calls[0]["extra_body"]["thinking"], {"type": "enabled"})
        self.assertEqual(calls[0]["extra_body"]["reasoning_effort"], "high")
        self.assertNotIn("temperature", calls[0])

    def test_reasoning_recovery_uses_non_thinking_and_reasoning_only(self):
        calls = []

        class Completions:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                return iter(
                    [
                        SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    delta=SimpleNamespace(
                                        content="根据文档可以推断为Ⅱ类。[C1]"
                                    )
                                )
                            ]
                        )
                    ]
                )

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        reasoning = "分析过程中曾有不确定候选。\n最终答案：根据文档可以推断为Ⅱ类。"
        with patch("service.core.generation._client", return_value=client):
            answer = recover_answer_from_reasoning(reasoning)

        self.assertEqual(answer, "根据文档可以推断为Ⅱ类。[C1]")
        self.assertEqual(calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertTrue(calls[0]["stream"])
        self.assertEqual(calls[0]["temperature"], 0)
        prompt = calls[0]["messages"][0]["content"]
        self.assertIn("根据文档可以推断为Ⅱ类。", prompt)
        self.assertNotIn("分析过程中曾有不确定候选", prompt)
        self.assertNotIn("<evidence>", prompt)
        self.assertIn("不重新检查最终结论", prompt)

    def test_explicit_cited_reasoning_answer_is_returned_directly(self):
        reasoning = (
            "前面存在多个候选。\n"
            "根据文档可以推断，最终属于Ⅱ类竞赛。因为满足对应条件 **[C5][C8]**。"
        )
        with patch("service.core.generation._client") as client:
            answer = recover_answer_from_reasoning(reasoning)

        self.assertEqual(
            answer,
            "根据文档可以推断，最终属于Ⅱ类竞赛。因为满足对应条件 [C5][C8]。",
        )
        client.assert_not_called()

    def test_reasoning_only_response_is_recovered_and_streamed(self):
        document = {
            "citation_id": "C1",
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "document_name": "办法",
            "content_with_weight": "目录内且不属于前一类别的项目属于Ⅱ类。",
        }
        reasoning = "分析完成。根据文档可以推断该项目属于Ⅱ类。"
        recovered = "根据文档可以推断，该项目属于Ⅱ类。[C1]"
        with patch(
            "service.core.chat.stream_grounded_draft",
            return_value=iter([("reasoning", reasoning)]),
        ), patch(
            "service.core.chat.stream_recovered_answer_from_reasoning",
            return_value=iter(["根据文档可以推断，", "该项目属于Ⅱ类。[C1]"]),
        ) as recover, patch(
            "service.core.chat.generate_recommended_questions", return_value=[]
        ), patch(
            "service.core.chat.update_session_name"
        ), patch(
            "service.core.chat.write_chat_to_db"
        ):
            events = list(
                get_chat_completion(
                    "session-a",
                    "属于哪一类？",
                    [document],
                    "user-a",
                    "request-a",
                    [],
                )
            )

        answer = "".join(
            json.loads(event.split("data: ", 1)[1])["content"]
            for event in events
            if '"thinking": false' in event
        )
        self.assertEqual(answer, recovered)
        self.assertEqual(recover.call_args.args[0], reasoning)

    def test_model_content_is_streamed_directly_and_displayed_thinking_is_saved(self):
        document = {
            "citation_id": "C1",
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "document_name": "条款",
            "content_with_weight": "意外医疗不受等待期限制。",
        }
        history = [{"user_question": "疾病医疗等待期多久？", "model_answer": "30天。"}]
        with patch(
            "service.core.chat.stream_grounded_draft",
            return_value=iter([("reasoning", "模型推理"), ("content", "未经校验的草稿[C1]")]),
        ), patch(
            "service.core.chat.generate_recommended_questions", return_value=[]
        ), patch(
            "service.core.chat.update_session_name"
        ), patch(
            "service.core.chat.write_chat_to_db"
        ) as write:
            events = list(
                get_chat_completion(
                    "session-a",
                    "那意外医疗呢？",
                    [document],
                    "user-a",
                    "request-a",
                    history,
                )
            )

        payloads = []
        for event in events:
            if "data: {" in event:
                payloads.append(json.loads(event.split("data: ", 1)[1]))
        thinking = [item for item in payloads if item.get("thinking") is True]
        answers = [item for item in payloads if item.get("thinking") is False]
        self.assertEqual("".join(item["content"] for item in thinking), "模型推理")
        self.assertEqual("".join(item["content"] for item in answers), "未经校验的草稿[C1]")
        self.mark_warning.assert_not_called()
        self.assertEqual(write.call_args.args[2], "未经校验的草稿[C1]")
        self.assertEqual(write.call_args.args[-1], "模型推理")

    def test_invalid_citations_are_reported_without_rewriting_streamed_answer(self):
        document = {
            "citation_id": "C1",
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "document_name": "条款",
            "content_with_weight": "支持内容。",
        }
        with patch(
            "service.core.chat.stream_grounded_draft",
            return_value=iter([("content", "模型原始答案。[C9]")]),
        ), patch(
            "service.core.chat.generate_recommended_questions", return_value=[]
        ), patch(
            "service.core.chat.update_session_name"
        ), patch(
            "service.core.chat.write_chat_to_db"
        ) as write:
            events = list(
                get_chat_completion(
                    "session-a", "问题", [document], "user-a", "request-a", []
                )
            )

        payloads = [
            json.loads(event.split("data: ", 1)[1])
            for event in events
            if "data: {" in event
        ]
        answer = "".join(
            item.get("content", "")
            for item in payloads
            if item.get("thinking") is False
        )
        self.assertEqual(answer, "模型原始答案。[C9]")
        self.mark_warning.assert_called_once_with("user-a", "session-a")
        self.clear_warning.assert_not_called()
        self.assertEqual(write.call_args.args[2], answer)
        self.assertEqual(write.call_args.args[3], [])

    def test_previous_citation_warning_is_injected_once_and_cleared_after_valid_answer(self):
        document = {
            "citation_id": "C1",
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "document_name": "条款",
            "content_with_weight": "支持内容。",
        }
        self.has_warning.return_value = True
        with patch(
            "service.core.chat.stream_grounded_draft",
            return_value=iter([("content", "合法答案。[C1]")]),
        ) as stream, patch(
            "service.core.chat.generate_recommended_questions", return_value=[]
        ), patch(
            "service.core.chat.update_session_name"
        ), patch(
            "service.core.chat.write_chat_to_db"
        ):
            list(
                get_chat_completion(
                    "session-a", "问题", [document], "user-a", "request-a", []
                )
            )

        self.assertTrue(stream.call_args.args[3])
        self.clear_warning.assert_called_once_with("user-a", "session-a")
        self.mark_warning.assert_not_called()

    def test_empty_model_response_emits_error_and_is_not_saved(self):
        document = {
            "citation_id": "C1",
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "document_name": "条款",
            "content_with_weight": "支持内容。",
        }
        with patch(
            "service.core.chat.stream_grounded_draft", return_value=iter([])
        ), patch("service.core.chat.write_chat_to_db") as write:
            events = list(
                get_chat_completion(
                    "session-a", "问题", [document], "user-a", "request-a", []
                )
            )

        self.assertTrue(any(event.startswith("event: error") for event in events))
        write.assert_not_called()

    def test_database_persists_actual_displayed_thinking(self):
        fake_db = FakeDatabase()
        with patch("service.core.chat.SessionLocal", return_value=fake_db):
            write_chat_to_db("session-a", "问题", "答案", [], [], "真实推理内容")
        self.assertTrue(fake_db.committed)
        self.assertEqual(fake_db.calls[0][1]["think"], "真实推理内容")


if __name__ == "__main__":
    unittest.main()
