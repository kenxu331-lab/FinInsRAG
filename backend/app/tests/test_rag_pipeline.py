from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from service.core.context import pack_context
from service.core.generation import (
    NO_ANSWER,
    _draft_prompt,
    finalize_grounded_answer,
    generate_grounded_answer,
    validate_citation_syntax,
)
from service.core.ingestion import (
    _build_chunks,
    _index_chunks,
    _split_structured_text,
    count_tokens,
    ingest_document,
    parse_blocks,
    structure_aware_chunks,
)
from models.document import Document
from service.core.retrieval import (
    RetrievalUnavailable,
    bm25_recall,
    deduplicate_candidates,
    dense_recall,
    reciprocal_rank_fusion,
    rerank_candidates,
)
from service.core.types import Evidence, ParsedBlock, RecallHit


class FakeES:
    def __init__(self):
        self.bodies = []

    def search(self, **kwargs):
        self.bodies.append(kwargs["body"])
        return {"hits": {"hits": []}}


class MissingIndexES:
    class Indices:
        @staticmethod
        def exists(**_kwargs):
            return False

    indices = Indices()


class ChunkingTests(unittest.TestCase):
    def test_headings_table_header_unit_and_note_are_preserved(self):
        text = """# 安心医疗保险条款

## 保障责任

一般医疗按条款给付。

| 责任类别 | 年限额 |
| --- | --- |
| 一般医疗 | 100万元 |

注：单位为人民币元。"""
        parsed = _split_structured_text(text, [])
        self.assertEqual(
            [item.block_type for item in parsed],
            ["heading", "heading", "paragraph", "table"],
        )
        self.assertEqual(parsed[2].heading_path, ["安心医疗保险条款", "保障责任"])
        self.assertIn("| 责任类别 | 年限额 |", parsed[3].text)
        self.assertIn("| 一般医疗 | 100万元 |", parsed[3].text)
        self.assertIn("注：单位为人民币元。", parsed[3].text)

        chunks = structure_aware_chunks(parsed)
        self.assertEqual([item.block_type for item in chunks], ["paragraph", "table"])
        self.assertTrue(all(item.heading_path == ["安心医疗保险条款", "保障责任"] for item in chunks))

    def test_table_row_is_not_split(self):
        table = "|项目|金额|\n|---|---|\n|住院|100万元|"
        chunks = structure_aware_chunks([ParsedBlock(text=table, block_type="table")])
        self.assertEqual([item.text for item in chunks], [table])

    def test_complete_sentences_are_preserved(self):
        sentence = "等待期为三十天。"
        text = sentence * 300
        chunks = structure_aware_chunks([ParsedBlock(text=text)])
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(item.text.endswith("。") for item in chunks))

    def test_glued_chapter_after_article_is_split(self):
        parsed = _split_structured_text(
            "第四条 本条正文。 第三章 组织管理机构与职责",
            [(1, "第二章 分类与级别")],
        )
        self.assertEqual([item.block_type for item in parsed], ["paragraph", "heading"])
        self.assertEqual(parsed[0].heading_path, ["第二章 分类与级别"])
        self.assertEqual(parsed[1].heading_path, ["第三章 组织管理机构与职责"])

    def test_pdf_layout_title_hint_does_not_promote_a_complete_sentence(self):
        parsed = _split_structured_text(
            "七、主合同列明的其他责任免除事项。",
            [(1, "保险条款"), (2, "第五条责任免除")],
            block_type_hint="heading",
        )
        self.assertEqual([item.block_type for item in parsed], ["paragraph"])
        self.assertEqual(parsed[0].heading_path, ["保险条款", "第五条责任免除"])

    def test_docx_raw_parser_output_reaches_structure_stage_before_chunking(self):
        class FakeDocx:
            def __call__(self, _file_path):
                return (
                    [
                        ("第一章 总则", None),
                        ("第一条 本办法适用于所有参赛研究生。", None),
                        ("第二条 本条正文。 第二章 奖励办法", None),
                        ("第三条 获奖后发放奖金。", None),
                    ],
                    [((None, "<table><tr><td>奖项</td><td>金额</td></tr></table>"), "")],
                )

        with patch("service.core.rag.app.naive.Docx", return_value=FakeDocx()), patch(
            "service.core.rag.app.naive.chunk",
            side_effect=AssertionError("DOCX must not enter the legacy pre-merge chain"),
        ):
            parsed = parse_blocks("policy.docx")

        self.assertEqual(parsed[1].heading_path, ["第一章 总则"])
        self.assertEqual(parsed[3].heading_path, ["第二章 奖励办法"])
        self.assertEqual(parsed[-1].block_type, "table")
        self.assertEqual(parsed[-1].table_id, "table-1")
        chunks = structure_aware_chunks(parsed)
        self.assertFalse(any(item.block_type == "heading" for item in chunks))

    def test_pdf_layout_page_and_table_metadata_reach_structure_stage(self):
        class FakePdf:
            boxes = [
                {"layout_type": "title"},
                {"layout_type": "title"},
                {"layout_type": "text"},
            ]

            def __call__(self, _file_path, callback):
                callback(msg="parsed")
                return (
                    [
                        ("乐学无忧保险条款", "@@1\t10.0\t100.0\t10.0\t18.0##"),
                        ("保险责任", "@@1\t10.0\t100.0\t20.0\t28.0##"),
                        ("等待期为三十天。", "@@1\t10.0\t100.0\t30.0\t38.0##"),
                    ],
                    [
                        (
                            (None, "<table><tr><td>项目</td><td>限额</td></tr></table>"),
                            [(0, 10.0, 100.0, 40.0, 60.0)],
                        )
                    ],
                )

        with patch("service.core.rag.app.naive.Pdf", return_value=FakePdf()), patch(
            "service.core.rag.app.naive.chunk",
            side_effect=AssertionError("PDF must not enter the legacy pre-merge chain"),
        ):
            parsed = parse_blocks("policy.pdf")

        evidence = [item for item in parsed if item.block_type != "heading"]
        self.assertEqual(evidence[0].heading_path, ["保险责任"])
        self.assertEqual(evidence[0].page, 1)
        self.assertEqual(evidence[0].positions[0], [1, 10, 100, 30, 38])
        self.assertEqual(evidence[1].block_type, "table")
        self.assertEqual(evidence[1].heading_path, ["保险责任"])
        self.assertEqual(evidence[1].table_id, "table-1")


class RetrievalTests(unittest.TestCase):
    def test_missing_index_is_an_explicit_service_error(self):
        with patch("service.core.retrieval._es_client", return_value=MissingIndexES()):
            with self.assertRaises(RetrievalUnavailable):
                from service.core.retrieval import hybrid_retrieve

                hybrid_retrieve("u1", "s1", "等待期")

    def test_bm25_and_dense_are_independent_queries(self):
        fake = FakeES()
        with patch("service.core.retrieval._es_client", return_value=fake):
            bm25_recall("u1", "s1", "等待期")
        self.assertIn("query", fake.bodies[0])
        self.assertNotIn("knn", fake.bodies[0])

        fake = FakeES()
        with patch("service.core.retrieval._es_client", return_value=fake), patch(
            "service.core.retrieval._generate_query_embedding", return_value=[0.1] * 1024
        ):
            dense_recall("u1", "s1", "多久可以理赔")
        self.assertIn("knn", fake.bodies[0])
        self.assertNotIn("query", fake.bodies[0])

    def test_rrf_uses_rank_not_raw_score(self):
        source = {"document_id": "d1", "content": "a"}
        fused = reciprocal_rank_fusion(
            [
                [RecallHit("a", source, 1, 1000, "bm25"), RecallHit("b", source, 2, 900, "bm25")],
                [RecallHit("b", source, 1, 0.7, "dense")],
            ]
        )
        self.assertEqual(fused[0]["chunk_id"], "b")
        self.assertEqual(fused[0]["retrieval_sources"], ["bm25", "dense"])

    def test_exact_and_near_duplicates_are_removed(self):
        candidates = [
            {"chunk_id": "1", "source": {"chunk_hash": "x", "content": "等待期为30天"}},
            {"chunk_id": "2", "source": {"chunk_hash": "x", "content": "等待期为30天"}},
            {"chunk_id": "3", "source": {"chunk_hash": "y", "content": "完全不同的理赔材料"}},
        ]
        self.assertEqual([item["chunk_id"] for item in deduplicate_candidates(candidates)], ["1", "3"])

    def test_rerank_uses_provider_order_and_scores(self):
        candidates = [
            {"chunk_id": "a", "source": {"document_name": "a", "content": "A"}},
            {"chunk_id": "b", "source": {"document_name": "b", "content": "B"}},
            {"chunk_id": "c", "source": {"document_name": "c", "content": "C"}},
        ]
        rerank_settings = SimpleNamespace(
            rerank_enabled=True,
            rerank_top_k=2,
            rerank_model="gte-rerank-v2",
        )
        response = {
            "output": {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.4},
                ]
            }
        }
        with patch("service.core.retrieval.settings", rerank_settings), patch(
            "service.core.retrieval._call_rerank_api", return_value=response
        ):
            reranked = rerank_candidates("问题", candidates)

        self.assertEqual([item["chunk_id"] for item in reranked], ["b", "a"])
        self.assertEqual([item["rerank_rank"] for item in reranked], [1, 2])
        self.assertEqual(reranked[0]["rerank_score"], 0.9)

    def test_rerank_failure_falls_back_to_rrf_top_k(self):
        candidates = [
            {"chunk_id": "a", "source": {"content": "A"}},
            {"chunk_id": "b", "source": {"content": "B"}},
            {"chunk_id": "c", "source": {"content": "C"}},
        ]
        rerank_settings = SimpleNamespace(
            rerank_enabled=True,
            rerank_top_k=2,
            rerank_model="gte-rerank-v2",
        )
        with patch("service.core.retrieval.settings", rerank_settings), patch(
            "service.core.retrieval._call_rerank_api", side_effect=TimeoutError("slow")
        ):
            reranked = rerank_candidates("问题", candidates)

        self.assertEqual([item["chunk_id"] for item in reranked], ["a", "b"])


class IndexingTests(unittest.TestCase):
    def test_structured_heading_reaches_document_chunk_and_es_bulk_action(self):
        document = SimpleNamespace(
            document_id="d1",
            user_id="u1",
            session_id=None,
            scope="user",
            file_name="保险条款.pdf",
        )
        blocks = [
            ParsedBlock(
                text="等待期为三十天。",
                block_type="paragraph",
                heading_path=["保险条款", "第四条保险责任"],
                page=2,
                positions=[[2, 10, 100, 20, 40]],
            )
        ]
        with patch(
            "service.core.rag.nlp.model.generate_embedding",
            return_value=[[0.1] * 1024],
        ):
            chunks = _build_chunks(blocks, document, None)

        source = chunks[0].to_index_document()
        self.assertEqual(source["heading_path"], ["保险条款", "第四条保险责任"])
        self.assertEqual(source["page"], 2)
        self.assertEqual(source["positions"], [[2, 10, 100, 20, 40]])
        self.assertTrue(source["heading_tokens"])
        self.assertEqual(len(source["embedding"]), 1024)

        with patch("service.core.ingestion.ensure_rag_index"), patch(
            "service.core.ingestion._es_client", return_value=object()
        ), patch("service.core.ingestion.bulk") as bulk:
            _index_chunks(chunks)
        action = bulk.call_args.args[1][0]
        self.assertEqual(action["_source"]["heading_path"], source["heading_path"])
        self.assertEqual(action["_source"]["content"], "等待期为三十天。")

    def test_exact_upload_is_reused_and_changed_same_name_replaces_old_version(self):
        engine = create_engine("sqlite:///:memory:")
        Document.__table__.create(bind=engine)
        testing_session = sessionmaker(bind=engine)
        db = testing_session()

        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.docx"
            duplicate_path = Path(directory) / "duplicate.docx"
            replacement_path = Path(directory) / "replacement.docx"
            first_path.write_bytes(b"same-file")
            duplicate_path.write_bytes(b"same-file")
            replacement_path.write_bytes(b"changed-file")

            def parsed(path: str):
                return [ParsedBlock(text=Path(path).read_bytes().decode("ascii"))]

            with patch("service.core.ingestion.cleanup_expired_documents", return_value=0), patch(
                "service.core.ingestion.parse_blocks", side_effect=parsed
            ), patch("service.core.ingestion._build_chunks", return_value=[object()]), patch(
                "service.core.ingestion._index_chunks"
            ), patch("service.core.ingestion._es_client", return_value=MissingIndexES()):
                first = ingest_document(
                    file_path=str(first_path),
                    file_name="policy.docx",
                    user_id="u1",
                    db=db,
                )
                duplicate = ingest_document(
                    file_path=str(duplicate_path),
                    file_name="policy.docx",
                    user_id="u1",
                    db=db,
                )
                replacement = ingest_document(
                    file_path=str(replacement_path),
                    file_name="policy.docx",
                    user_id="u1",
                    db=db,
                )

        self.assertFalse(first["deduplicated"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["document_id"], first["document_id"])
        self.assertFalse(replacement["deduplicated"])
        self.assertEqual(replacement["replaced_count"], 1)
        ready = db.query(Document).filter(Document.parse_status == "ready").all()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].document_id, replacement["document_id"])
        self.assertEqual(ready[0].chunk_count, 1)
        self.assertIsNotNone(ready[0].file_hash)
        db.close()


class ContextAndCitationTests(unittest.TestCase):
    def test_generation_prompt_defines_mixed_source_inference_policy(self):
        evidence = [Evidence("C1", "x", "d", "doc", "目录内非Ⅰ类赛事属于Ⅱ类")]
        prompt = _draft_prompt("该赛事属于哪一类？", evidence)
        self.assertIn("来源A（证据集）", prompt)
        self.assertIn("来源B（通用常识）", prompt)
        self.assertIn("先用来源A的逻辑", prompt)
        self.assertIn("集合排除、分类闭环、隐式包含", prompt)
        self.assertIn("【常识补全声明】", prompt)
        self.assertIn("不要为来源B编造或附会证据引用", prompt)

    def test_generation_prompt_includes_previous_citation_warning(self):
        evidence = [Evidence("C1", "x", "d", "doc", "支持内容")]
        prompt = _draft_prompt("问题", evidence, citation_reminder=True)
        self.assertIn("上一轮回答的引用格式检查未通过", prompt)
        self.assertIn("不要输出不存在的引用编号", prompt)

    def test_local_finalizer_does_not_rewrite_model_output(self):
        evidence = [Evidence("C1", "x", "d", "doc", "目录内非Ⅰ类赛事属于Ⅱ类")]
        draft = "根据文档规则，可以合理推断该赛事更可能属于Ⅱ类。[C1]"
        with patch("service.core.generation._client") as client:
            answer = finalize_grounded_answer("该赛事属于哪一类？", evidence, draft)
        self.assertEqual(answer, draft)
        client.assert_not_called()

    def test_context_preserves_rerank_order(self):
        candidates = [
            {"chunk_id": "a1", "source": {"document_id": "a", "document_name": "a", "content": "A1"}, "rrf_score": 3},
            {"chunk_id": "a2", "source": {"document_id": "a", "document_name": "a", "content": "A2"}, "rrf_score": 2},
            {"chunk_id": "b1", "source": {"document_id": "b", "document_name": "b", "content": "B1"}, "rrf_score": 1},
        ]
        packed = pack_context(candidates, token_budget=1000)
        self.assertEqual([item.chunk_id for item in packed], ["a1", "a2", "b1"])
        self.assertEqual([item.citation_id for item in packed], ["C1", "C2", "C3"])

    def test_context_never_exceeds_budget(self):
        content = "证据" * 500
        candidates = [
            {"chunk_id": "a", "source": {"document_id": "a", "document_name": "a", "content": content}}
        ]
        budget = max(1, count_tokens(content) // 2)
        self.assertEqual(pack_context(candidates, token_budget=budget), [])

    def test_citation_gate(self):
        evidence = [Evidence("C1", "x", "d", "doc", "支持内容")]
        self.assertTrue(validate_citation_syntax("等待期为30天。[C1]", evidence))
        self.assertFalse(validate_citation_syntax("等待期为30天。", evidence))
        self.assertFalse(validate_citation_syntax("等待期为30天。[C2]", evidence))
        self.assertTrue(validate_citation_syntax(NO_ANSWER, evidence))

    def test_generation_preserves_model_output_without_automatic_citation_rewrite(self):
        evidence = [Evidence("C1", "x", "d", "doc", "等待期为30天，期满后生效。")]
        draft = "等待期为30天。[C1]\n\n期满后保障生效。"
        with patch("service.core.generation._draft", return_value=draft):
            answer = generate_grounded_answer("等待期多久？", evidence)
        self.assertEqual(answer, draft)
        self.assertFalse(validate_citation_syntax(answer, evidence))

    def test_no_evidence_returns_fixed_refusal(self):
        with patch("service.core.generation._draft") as draft:
            self.assertEqual(generate_grounded_answer("文档没有的问题", []), NO_ANSWER)
            draft.assert_not_called()


if __name__ == "__main__":
    unittest.main()
