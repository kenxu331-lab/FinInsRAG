# FinInsRAG V3

面向金融、保险文档的轻量 RAG 应用。后端采用 FastAPI、PostgreSQL、Elasticsearch 和 Redis；DeepSeek 负责生成，DashScope `text-embedding-v3` 负责 1024 维向量。

## 架构

```text
上传 → DeepDoc 解析 → 结构感知 Chunk → rag_chunks_v2
问题 → 独立 BM25 ─┐
                  ├→ RRF → 去重 → Context Packing
       独立 Dense ─┘          → 追问改写 → DeepSeek Thinking/草稿/校验 → [C1] 引用
```

关键约束：

- BM25 与 Dense 是两次独立 Elasticsearch 请求；Dense 不依赖文本命中。
- 融合只使用 RRF 排名，不进行二次本地加权。
- 表格行、列表项和完整句子不会因字符上限被硬切断。
- 持久文档按用户隔离；临时文档按用户和会话隔离，默认 24 小时过期。
- 回答只能使用打包证据；无证据时返回固定拒答，不使用常识补充。
- DeepSeek V4 Flash 原生 `reasoning_content` 通过 SSE 流式展示；最终答案在证据与引用校验通过后才输出。
- PostgreSQL 只读取最近 4 轮、约 2,000 tokens 的会话上下文；历史用于理解追问，不能充当本轮事实证据。

Thinking 与多轮上下文默认配置：

```dotenv
MODEL_NAME=deepseek-v4-flash
DEEPSEEK_THINKING_ENABLED=1
DEEPSEEK_REASONING_EFFORT=high
DEEPSEEK_REASONING_DISPLAY_MAX_CHARS=12000
RAG_HISTORY_TURNS=4
RAG_HISTORY_TOKEN_BUDGET=2000
```

校验、答案修复、追问改写、推荐问题和会话标题会显式关闭 Thinking，避免无意义的推理开销。历史轮次只回传用户问题和最终答案，不回传旧轮次的推理内容。

## 启动

1. 从 `.env.example` 创建 `.env`，填写 PostgreSQL、Elasticsearch、JWT、DeepSeek 和 DashScope 配置。
2. 已经在聊天或提交记录中暴露过的真实密钥应先在服务商后台轮换。
3. 验证并启动：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

默认地址：

- 后端与 API 文档：`http://localhost:8001/docs`
- 健康检查：`http://localhost:8001/health`
- Elasticsearch：`http://localhost:9201`

容器内部端口仍为后端 `8000`、Elasticsearch `9200`。前端开发环境统一连接 `http://localhost:8001`。

## 数据迁移

Alembic 会创建统一的 `documents` 表，旧 `knowledgebases` 和 `document_uploads` 表不会被破坏性删除。旧文件可在 `backend/app` 中重建到 v2 索引：

```bash
python scripts/reindex_v2.py --storage-root service/core/storage/file --dry-run
python scripts/reindex_v2.py --storage-root service/core/storage/file
```

脚本会报告总数、成功、去重、源文件缺失和失败数。切换由 `RAG_INDEX_NAME=rag_chunks_v2` 控制，旧 ES 索引保持不动。

## 测试和评测

```bash
cd app
python -m unittest discover -s tests -v
python -m compileall -q .

# 需要 PostgreSQL、Elasticsearch 和外部模型 API
python evaluation/run_eval.py --ingest-fixtures --user-id rag-eval-user
python evaluation/run_eval.py --user-id rag-eval-user --with-generation
```

`evaluation/rag_eval_40.jsonl` 包含关键词、语义改写、多跳、表格、否定/例外、冲突和无答案共 40 个标注问题。

最近一次使用全新结构化 Chunk、真实 DashScope/Elasticsearch/DeepSeek 的回归结果保存在
`app/evaluation/rag_eval_40_result.json`：Recall@20 为 100%，MRR@10 为 0.9625，答案要点命中率为
97.5%（39/40），引用 ID 合法率为 100%。评测结束后测试文档与 ES Chunk 已清理。

前端验证：

```bash
cd ../frontend
npm run build
```

## 接口兼容

- `POST /chat_on_docs?session_id=...` 请求体仍为 `{ "message": "..." }`，响应仍为 SSE。
- `documents` SSE 事件增加 `citation_id`、`chunk_id`、`page`、`heading_path`、`block_type` 和 `retrieval_sources`。
- `content_with_weight` 暂时保留，前端同时支持历史 `##1$$` 和新 `[C1]` 引用。
- 上传响应增加 `documents` 数组，其中包含 `document_id`、`deduplicated`、`chunk_count` 和 `parse_status`。

## 已知限制

- 扫描质量较差的 PDF、复杂跨页表格和合并单元格依赖 DeepDoc/OCR 识别质量，当前为尽力保留，不能保证所有文档 100% 还原。
- 文档级去重使用规范化全文 SHA-256；格式差异较大的近重复文档只会在召回结果阶段去重。
- 回答校验会增加一次模型调用延迟；校验服务不可用时只有通过确定性引用检查的草稿才会返回。
- 线上效果指标必须在目标业务文档的人工标注集上重新确认，仓库内 40 题仅用于回归基线。
