# 钱学森导师服务

独立管理固定 20 本资料的清单、分页解析、OCR、三层知识库、混合检索、只读 SQL 和 Framework Capability。

## 启动

```bash
uv sync
uv run python -m qianxuesen_mentor.infrastructure.migrate
uv run python -m qianxuesen_mentor.tools.import_corpus --catalog-only
uv run uvicorn qianxuesen_mentor.api:app --host 0.0.0.0 --port 18400
```

完整解析使用 `uv run python -m qianxuesen_mentor.tools.import_corpus --resume`。任务按页保存状态，可安全中断后重跑。原 PDF 始终只读。

扫描页增加 `--ocr`。PP-StructureV3 与主模型包对 PyYAML 的固定版本不兼容，因此仓库提供独立 `qianxuesen-mentor-ocr.Dockerfile`。部署时执行 `docker compose --profile qxs-ocr run --rm qianxuesen-mentor-ocr`；分页、检查点、结果入库和失败重试仍由本服务负责，避免把大型 OCR 运行时耦合到在线 API 容器。

### 源码 GPU OCR 调试

OCR 使用独立 Python 3.11 环境，不修改在线服务的 `.venv`：

```bash
uv venv --python 3.11 .venv-ocr
uv pip install --python .venv-ocr/bin/python paddlepaddle-gpu==3.3.0 \
  --index https://www.paddlepaddle.org.cn/packages/stable/cu130/
uv pip install --python .venv-ocr/bin/python paddleocr==3.7.0 \
  'paddlex[ocr]==3.7.2' 'psycopg[binary]>=3.2,<4' \
  'pydantic-settings>=2.4,<3' 'pypdf>=5,<7'
```

设置独立的 `QXS_DATABASE_URL`、`QXS_CORPUS_ROOT` 和 `QXS_OCR_DEVICE=gpu:0` 后执行：

```bash
PYTHONPATH=src .venv-ocr/bin/python -m qianxuesen_mentor.tools.import_corpus \
  --resume --ocr --no-embed
```

无文字的封面、插页单独记为 `blank`；非法 Unicode 代理码和 NUL 字符会先规范化。OCR、失败和中断均按页保存，可直接使用同一命令续跑。

### 单独生成 Embedding

OCR、切片和卡片完成后，使用独立命令回填向量；它不会重新读取 PDF，也不会重新运行 OCR：

```bash
QXS_MODEL_CONFIG_DIR=/path/to/model-config \
QXS_MODEL_SECRET_DIR=/path/to/model-config/secrets \
uv run python -m qianxuesen_mentor.tools.embed_corpus
```

命令复用主系统的模型包、Qwen Embedding API 和密钥文件。每 10 条请求一次并逐批提交，失败重试后仍可执行同一命令断点续跑。可先加 `--max-items 10` 验证 API 连通性；正式密钥只从密钥目录读取，不复制到本服务或日志。

服务 API：`/health`、`/v1/files`、`/v1/files/{id}/content`、`/v1/files/{id}/chunks`、`/v1/retrieval/search`、`/v1/query/sql`。

## 发布评测

`qianxuesen_mentor.evaluation` 固定提供 100 道题。完成全量导入后，可用 `python -m qianxuesen_mentor.tools.export_style_samples style.jsonl` 导出 30–50 条本人著作/书信风格样本。把 Framework 跑题结果保存为 JSONL 后，执行 `python -m qianxuesen_mentor.tools.evaluate_answers answers.jsonl`，会检查题目覆盖、伪造引语、页码引用核验、无依据拒答、语气禁用项和首字延迟门槛。
