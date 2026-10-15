"""使用 python-docx + OOXML 原始顺序导出 DOCX 结构化文本。

使用方式一：直接修改下面的 ``DOCX_PATH``，然后在 backend 目录运行：

    python app/scripts/parse_docx_with_ooxml.py

使用方式二：不修改脚本，通过命令行指定文件：

    python app/scripts/parse_docx_with_ooxml.py --input "D:\\文档\\示例.docx"

脚本按 ``word/document.xml`` 中的真实节点顺序交替遍历段落和表格；结合
Word 标题样式、outline level 及“第X章/第X条”等规则建立 heading_path。
“第X条”被标记为可回答的 article，而不是误当成只用于导航的章节标题。
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph


# ===== 手动配置区 =====
DOCX_PATH = Path(r"D:\Personal\Downloads\《大理大学研究生学科竞赛管理与奖励办法》.docx")
# 留空时输出到 DOCX 同目录，文件名后缀为 _ooxml_parsed.txt。
OUTPUT_PATH: Path | None = None
# ====================


CHINESE_NUMBER = "〇零一二两三四五六七八九十百千万0-9"
CHAPTER_PATTERN = re.compile(rf"^(第[{CHINESE_NUMBER}]+[编篇章])(?:[\s、：:.．]|$)")
SECTION_PATTERN = re.compile(rf"^(第[{CHINESE_NUMBER}]+节)(?:[\s、：:.．]|$)")
ARTICLE_PATTERN = re.compile(rf"^(第[{CHINESE_NUMBER}]+[条款])(?:[\s、：:.．]|$)")
STRUCTURAL_MARKER_PATTERN = re.compile(rf"第[{CHINESE_NUMBER}]+[编篇章节条款]")
LIST_PATTERN = re.compile(
    rf"^(?:[（(]?[{CHINESE_NUMBER}]+[）)、.]|\d+(?:\.\d+)*[、.)]|[-•●▪])\s*"
)


@dataclass
class ExportBlock:
    block_type: str
    text: str
    heading_path: list[str] = field(default_factory=list)
    style_name: str = ""
    style_id: str = ""
    heading_level: int | None = None


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 python-docx + OOXML 顺序解析 DOCX")
    parser.add_argument("--input", type=Path, help="DOCX 文件路径；覆盖脚本顶部 DOCX_PATH")
    parser.add_argument("--output", type=Path, help="TXT 输出路径；覆盖脚本顶部 OUTPUT_PATH")
    parser.add_argument("--plain", action="store_true", help="只输出正文，不输出结构元数据")
    return parser.parse_args()


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    source = (args.input or DOCX_PATH).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"DOCX 文件不存在：{source}")
    if source.suffix.lower() != ".docx":
        raise ValueError(f"仅支持 .docx 文件：{source}")

    configured_output = args.output or OUTPUT_PATH
    output = (
        configured_output.expanduser().resolve()
        if configured_output is not None
        else source.with_name(f"{source.stem}_ooxml_parsed.txt")
    )
    return source, output


def iter_body_blocks(document: DocumentObject) -> Iterable[Paragraph | Table]:
    """按 document.xml 中的顺序交替产出顶层段落和表格。"""

    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield Table(child, document)


def _clean_text(value: str) -> str:
    lines = []
    for line in (value or "").replace("\r", "\n").split("\n"):
        cleaned = re.sub(r"[\t\u00a0 ]+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _split_inline_structures(text: str) -> list[str]:
    """拆出句末误粘在同一 Word 段落中的“第X章/第X条”。

    只有标记位于行首，或前一个字符是空白/句末标点时才拆分，避免把
    “依据本文第六条”这样的正文引用误判成新的条款。
    """

    starts = [0]
    for match in STRUCTURAL_MARKER_PATTERN.finditer(text):
        start = match.start()
        if start == 0:
            continue
        if text[start - 1].isspace() or text[start - 1] in "。；;！？!?：:":
            starts.append(start)
    starts = sorted(set(starts))
    sections = [text[start:end].strip() for start, end in zip(starts, starts[1:] + [len(text)])]
    return [section for section in sections if section]


def _paragraph_lines(paragraph: Paragraph) -> list[str]:
    """保留 Word 软换行，并拆出同一段落内粘连的章/条结构。"""

    values: list[str] = []
    raw_value = (paragraph.text or "").replace("\r", "\n")
    for raw_line in raw_value.split("\n"):
        cleaned = _clean_text(raw_line)
        if cleaned:
            values.extend(_split_inline_structures(cleaned))
    return values


def _outline_level(paragraph: Paragraph) -> int | None:
    paragraph_properties = paragraph._p.pPr
    if paragraph_properties is None:
        return None
    outline = paragraph_properties.find(qn("w:outlineLvl"))
    if outline is None:
        return None
    raw_value = outline.get(qn("w:val"))
    if raw_value is None:
        return None
    try:
        # OOXML 从 0 开始，输出统一从 1 开始。
        return int(raw_value) + 1
    except ValueError:
        return None


def _style_heading_level(paragraph: Paragraph) -> int | None:
    style_name = paragraph.style.name if paragraph.style is not None else ""
    style_id = paragraph.style.style_id if paragraph.style is not None else ""
    for value in (style_name, style_id):
        match = re.search(r"(?:Heading|标题)\s*([1-9])", value or "", re.IGNORECASE)
        if match:
            return int(match.group(1))
    return _outline_level(paragraph)


def _is_title_style(paragraph: Paragraph) -> bool:
    if paragraph.style is None:
        return False
    values = {paragraph.style.name.lower(), paragraph.style.style_id.lower()}
    return any(value in {"title", "document title", "标题", "文档标题"} for value in values)


def _looks_like_document_title(text: str) -> bool:
    """处理使用 Normal 样式的常见中文公文标题。"""

    return (
        2 <= len(text) <= 80
        and not STRUCTURAL_MARKER_PATTERN.match(text)
        and not LIST_PATTERN.match(text)
        and text[-1] not in "。；;！？!?：:"
    )


def _replace_heading(stack: list[tuple[int, str]], level: int, title: str) -> None:
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))


def _table_to_markdown(table: Table) -> str:
    """导出为 Markdown；合并单元格的重复引用只在首次出现时写入内容。"""

    rows: list[list[str]] = []
    seen_cells: set[int] = set()
    for row in table.rows:
        values: list[str] = []
        for cell in row.cells:
            cell_key = id(cell._tc)
            if cell_key in seen_cells:
                value = ""
            else:
                seen_cells.add(cell_key)
                value = _clean_text(cell.text).replace("\n", "<br>").replace("|", "\\|")
            values.append(value)
        rows.append(values)

    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = "| " + " | ".join(rows[0]) + " |"
    separator = "| " + " | ".join(["---"] * width) + " |"
    body = ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join([header, separator, *body])


def parse_docx(source: Path) -> list[ExportBlock]:
    document = Document(str(source))
    result: list[ExportBlock] = []
    heading_stack: list[tuple[int, str]] = []

    for body_block in iter_body_blocks(document):
        if isinstance(body_block, Table):
            table_text = _table_to_markdown(body_block)
            if table_text:
                result.append(
                    ExportBlock(
                        block_type="table",
                        text=table_text,
                        heading_path=[title for _, title in heading_stack],
                    )
                )
            continue

        paragraph = body_block
        style_name = paragraph.style.name if paragraph.style is not None else ""
        style_id = paragraph.style.style_id if paragraph.style is not None else ""
        style_level = _style_heading_level(paragraph)

        for text in _paragraph_lines(paragraph):
            if _is_title_style(paragraph) or (not result and _looks_like_document_title(text)):
                result.append(
                    ExportBlock(
                        block_type="document_title",
                        text=text,
                        style_name=style_name,
                        style_id=style_id,
                    )
                )
                continue

            chapter = CHAPTER_PATTERN.match(text)
            section = SECTION_PATTERN.match(text)
            article = ARTICLE_PATTERN.match(text)

            if chapter:
                _replace_heading(heading_stack, 1, text)
                block_type = "heading"
                heading_level = 1
            elif section:
                _replace_heading(heading_stack, 2, text)
                block_type = "heading"
                heading_level = 2
            elif article:
                # 条款正文仍然是证据；heading_path 只放“第三条”这类稳定标识，
                # 不把整条正文重复塞入后续块的检索元数据。
                _replace_heading(heading_stack, 3, article.group(1))
                block_type = "article"
                heading_level = 3
            elif style_level is not None:
                _replace_heading(heading_stack, style_level, text)
                block_type = "heading"
                heading_level = style_level
            else:
                block_type = "list" if LIST_PATTERN.match(text) else "paragraph"
                heading_level = None

            result.append(
                ExportBlock(
                    block_type=block_type,
                    text=text,
                    heading_path=[title for _, title in heading_stack],
                    style_name=style_name,
                    style_id=style_id,
                    heading_level=heading_level,
                )
            )
    return result


def _render_annotated(source: Path, blocks: list[ExportBlock]) -> str:
    sections = [
        "python-docx + OOXML 顺序解析结果",
        f"source: {source}",
        f"block_count: {len(blocks)}",
        "说明：段落和表格按 document.xml 原始顺序输出；第X条作为 article 保留正文。",
    ]
    for index, block in enumerate(blocks, start=1):
        heading_path = " > ".join(block.heading_path) or "(empty)"
        sections.append(
            "\n".join(
                [
                    f"===== OOXML Block {index:03d} =====",
                    f"block_type: {block.block_type}",
                    f"heading_path: {heading_path}",
                    f"heading_level: {block.heading_level}",
                    f"style_name: {block.style_name or '(empty)'}",
                    f"style_id: {block.style_id or '(empty)'}",
                    "text:",
                    block.text,
                ]
            )
        )
    return "\n\n".join(sections).rstrip() + "\n"


def _render_plain(blocks: list[ExportBlock]) -> str:
    return "\n\n".join(block.text for block in blocks if block.text).rstrip() + "\n"


def main() -> None:
    args = _arguments()
    source, output = _resolve_paths(args)
    blocks = parse_docx(source)
    content = _render_plain(blocks) if args.plain else _render_annotated(source, blocks)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8-sig")

    print(f"解析完成：{len(blocks)} 个结构块")
    print(f"输出文件：{output}")


if __name__ == "__main__":
    main()
