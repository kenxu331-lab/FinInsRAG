"""使用 FinInsRAG 当前解析链导出 DOCX 解析结果。

使用方式一：直接修改下面的 ``DOCX_PATH``，然后在 backend 目录运行：

    python app/scripts/parse_docx_with_project.py

使用方式二：不修改脚本，通过命令行指定文件：

    python app/scripts/parse_docx_with_project.py --input "D:\\文档\\示例.docx"

默认输出带 ParsedBlock 元数据的 TXT；加 ``--plain`` 只输出正文。
本脚本复用生产代码中的 ``service.core.ingestion.parse_blocks``，因此展示的
是当前项目实际得到的 ParsedBlock，而不是另写一套近似解析逻辑。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


# ===== 手动配置区 =====
DOCX_PATH = Path(r"D:\Personal\Downloads\《大理大学研究生学科竞赛管理与奖励办法》.docx")
# 留空时输出到 DOCX 同目录，文件名后缀为 _project_parsed.txt。
OUTPUT_PATH: Path | None = None
# ====================


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用项目当前解析链导出 DOCX")
    parser.add_argument("--input", type=Path, help="DOCX 文件路径；覆盖脚本顶部 DOCX_PATH")
    parser.add_argument("--output", type=Path, help="TXT 输出路径；覆盖脚本顶部 OUTPUT_PATH")
    parser.add_argument("--plain", action="store_true", help="只输出块正文，不输出结构元数据")
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
        else source.with_name(f"{source.stem}_project_parsed.txt")
    )
    return source, output


def _render_annotated(source: Path, blocks: list[object]) -> str:
    sections = [
        "FinInsRAG 当前 DOCX 解析结果",
        f"source: {source}",
        f"parsed_block_count: {len(blocks)}",
        "说明：heading_path/page/table_id 均为项目当前解析链的真实输出。",
    ]

    for index, block in enumerate(blocks, start=1):
        heading_path = " > ".join(getattr(block, "heading_path", []) or []) or "(empty)"
        positions = getattr(block, "positions", []) or []
        sections.append(
            "\n".join(
                [
                    f"===== ParsedBlock {index:03d} =====",
                    f"block_type: {getattr(block, 'block_type', 'paragraph')}",
                    f"heading_path: {heading_path}",
                    f"page: {getattr(block, 'page', None)}",
                    f"table_id: {getattr(block, 'table_id', None)}",
                    f"positions: {positions}",
                    "text:",
                    str(getattr(block, "text", "")),
                ]
            )
        )
    return "\n\n".join(sections).rstrip() + "\n"


def _render_plain(blocks: list[object]) -> str:
    texts = [str(getattr(block, "text", "")).strip() for block in blocks]
    return "\n\n".join(text for text in texts if text).rstrip() + "\n"


def main() -> None:
    args = _arguments()
    source, output = _resolve_paths(args)

    # 延迟导入，路径配置错误时可以先给出更直接的错误信息。
    from service.core.ingestion import parse_blocks

    blocks = parse_blocks(str(source))
    content = _render_plain(blocks) if args.plain else _render_annotated(source, blocks)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8-sig")

    print(f"解析完成：{len(blocks)} 个 ParsedBlock")
    print(f"输出文件：{output}")


if __name__ == "__main__":
    main()
