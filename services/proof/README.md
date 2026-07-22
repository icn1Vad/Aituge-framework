# Proof service

`proof` 是独立于 framework 和旧 `services/proofreading` 的制度知识服务。当前版本完成
PDF、DOCX、Markdown、TXT 同步入库，使用确定性的预制结构模板生成 retrieval unit，并为以后
接入外部 embedding、pgvector、中文全文检索和 Qwen Reranker；结构识别不调用大模型。

## 问答检索架构

```text
Proof Agent / proof_search
        │
        ├── pg_jieba 中文全文 Top 30 ──┐
        └── embedding + pgvector Top 30 ├── text_hash 去重 ── qwen3-rerank ── 完整条款 + citation.label
                                      ┘

Proof Agent / proof_sql
        └── Agent 生成 SELECT ── 只读执行器 ── proof_sql_policy_v / proof_sql_clause_v
```

召回路径并行且可以独立降级：Reranker 不可用时返回融合排序；全文或向量单路不可用时
继续使用另一条路径；两路都不可用才返回 `retrieval_unavailable`。Reranker 接收完整条款，
由供应商在排序计算内部自动截断；Proof 不生成临时子 Chunk，入库正文和返回正文始终完整。

检索实现按职责拆分：`application/retrieval.py` 只做编排和融合，
`infrastructure/retrieval.py` 连接 PostgreSQL/pgvector，`infrastructure/reranking.py` 连接
DashScope，`application/sql_query.py` 管理 SQL 执行边界。Framework 不包含这些业务实现。

## 结构识别与切分规则

- `article`：一个完整“第X条”就是一个 retrieval unit，支持中文数字、阿拉伯数字和编号内部空格；
- `decimal_outline`：识别 `1 → 1.1 → 1.1.1`。通常以稳定的 `x.y` 层为 unit，`x` 作为标题路径，
  更深层编号留在父 unit；没有子条的一级节点自身成为 unit；
- `chinese_outline`：识别 `一、→（一）→1.→（1）`，通常以“（一）”层为 unit，
  更低层序号留在父 unit；没有子项的“一、”节点自身成为 unit；
- `mixed`：多个独立区域分别套用自己的模板并按原文顺序合并，同时产生明确的混合结构提示；
- “第X条”区域拥有其内部的 `一、/（一）/1.` 等子项，不会因为子项编号再次拆分；
- 编号正则只负责识别候选，是否成为边界还会检查连续序列、层级、区域、标题和缩进元数据；
- 章、节和上级编号只进入 `heading_path`，不单独入检索单元；
- unit 内段落、子项、列表、跨页文本和表格保持完整；
- 不按字数、句子或段落二次切分，也没有 overlap；
- 目录点线、页脚和正文前说明不作为检索边界；
- 找不到能形成稳定序列的支持结构时返回 `422 no_clauses_found`，数据库和文件存储都不留残数据；
- embedding 输入过长时标记 `embedding_too_long`，仍不拆开条款。

每份文档保存 `structure_profile` 和 `structure_diagnostics`，包括识别区域、模板、结构覆盖率、
重复编号和混合编号提示。每个 unit 保存自己的 `unit_type`。

## 本地启动

先启动外层数据容器，然后进入本目录安装依赖：

```bash
cd infrastructure
docker compose --env-file .env -f compose.data.yml up -d

cd ../services/proof
uv sync --all-groups
cp .env.example .env
```

在 `.env` 配置 `PROOF_DATABASE_URL`，首次运行 migration：

```bash
uv run python -m proof.infrastructure.postgres.migrate
uv run uvicorn proof.api.app:app --host 127.0.0.1 --port 18100
```

OpenAPI 文档位于 `http://127.0.0.1:18100/docs`。embedding 配置可以留空；此时上传、
切分、查看、fetch 和中文全文检索仍可用，混合检索会明确标记向量路径已降级。
`PROOF_RERANK_API_KEY` 留空时复用 embedding key。

## 挂载到 Aituge

Proof 自己维护 `capabilities/register.py` 和主 Skill，Framework 只提供受限注册表与执行内核。
启动 Aituge 前配置：

```bash
export AITUGE_CAPABILITY_ENTRY="$PWD/capabilities/register.py"
export PROOF_SERVICE_BASE_URL=http://127.0.0.1:18100
```

挂载后可通过 TaskManager 执行 `proof.qa.chat`。生产审校只创建一个 `proof.audit.run`，其内部并行
执行制度概览、清晰性/可执行性和制度冲突 Stage；`proof.conflict.audit` 仅保留用于诊断。该问答 Task 仍走现有
Scheduler 和 SingleAgent，仅注入 `proof_search`、`proof_sql`、`code_interpreter` 与
`proof-policy-qa`，不会使用 Framework 本地 RAG。

该审校流程默认关闭。启用前配置：

```bash
PROOF_SEMANTIC_AUDIT_ENABLED=true
PROOF_FRAMEWORK_BASE_URL=http://127.0.0.1:8894
PROOF_FRAMEWORK_USER_ID=proof-service
PROOF_FRAMEWORK_TENANT_ID=__default_tenant_id__
PROOF_AUDIT_MODEL_ID=deepseek-v4-pro
PROOF_CONFLICT_MODEL_ID=deepseek-v4-pro
PROOF_AUDIT_BATCH_MAX_CHARS=6000
PROOF_AUDIT_BATCH_MAX_CHUNKS=8
PROOF_AUDIT_MAX_CHUNK_CHARS=12000
PROOF_AUDIT_MAX_CONCURRENCY=4
```

`http://127.0.0.1:18100/` 是最小制度工作台，包含上传、结构检测、chunk 展开、
检查报告、制度仓库管理，以及全文/向量/Reranker 各阶段数量清晰可见的检索诊断。上传先
保存为草稿，制度概览、确定性结构检查、清晰性/可执行性和制度冲突审校合并展示，人工确认后才进入检索。审校 Finding 是
提示而不是自动阻断规则。页面内可取得同时包含语义歧义、可执行性缺口和跨制度冲突规则的
`采购管理制度（试行）.txt` 联合审校实验制度。

配置 `PROOF_DATASET_ROOT` 后，`http://127.0.0.1:18100/dataset` 提供数据集入库检查页，
可按数据组、层级、建议分类、结构模板和入库状态横向查看每份制度，并展开完整 chunk、
识别区域、结构覆盖率和异常提示。
数据集扫描是只读的；批量入库需要显式运行：

```bash
uv run python -m proof.tools.import_dataset --dry-run
uv run python -m proof.tools.import_dataset
```

结构引擎升级后，可只重建小数层级和混合结构文档的派生 blocks/units，保留 policy/document 身份：

```bash
uv run python -m proof.tools.reprocess_structures
```

全量结构验收会重新解析每个数据集文件，检查边界、source block 顺序与重叠、覆盖率，
并把逐条 chunk hash 与 PostgreSQL 对比。任何结构或数据库不一致都会以非零状态退出：

```bash
uv run python -m proof.tools.validate_dataset_chunks \
  --output .proof-data/reports/dataset-structure-validation.json
```

## API

- `POST /v1/policies`：multipart 上传并同步解析、切分、入库；
- `GET /v1/policies`：按层级和分类过滤；
- `GET /v1/policies/{policy_id}`：制度和文档状态；
- `GET /v1/policies/{policy_id}/clauses`：按原文顺序查看条款；
- `GET /v1/policies/{policy_id}/audit-status`：查询父审校、三个 Stage 状态及统一统计；
- `GET /v1/policies/{policy_id}/policy-summary`：返回制度初步分析；
- `GET /v1/policies/{policy_id}/semantic-findings`：返回结构、语义与可执行性结果；
- `GET /v1/policies/{policy_id}/conflict-findings`：返回制度冲突结果；
  `unavailable_candidate_ids` 标识已删除或当前不可用的候选 Chunk；
- `POST /v1/policies/{policy_id}/confirm`：审校完成后确认制度生效；
- `GET /v1/ingestion-runs/{run_id}`：查看本次入库、复用或失败的运行记录；
- `GET /v1/dataset/audit`：扫描固定数据集口径并返回分类、chunk 计数和异常；
- `GET /v1/dataset/files/{file_id}`：返回单个数据集文件的完整条款 chunk；
- `GET /v1/files`、`GET /v1/files/{file_id}/content`、`GET /v1/files/{file_id}/chunks`：查看库内文件及其内容；
- `GET /v1/categories/levels`：查询三个固定制度层级；
- `GET /v1/categories/policies`：查询制度分类；
- `POST /v1/documents/{document_id}/index`：调用外部 embedding 并写入 pgvector；
- `POST /v1/retrieval/search`：按完整条款做混合、向量或中文全文检索，支持制度、层级和分类过滤；
- `POST /v1/retrieval/fetch`：不依赖 embedding，按 unit ID 返回完整条款和 citation。
- `POST /v1/query/sql`：执行 Agent 生成的一条只读 `SELECT/WITH`，用于计数、列表、分组和精确过滤。

冲突审校内部工具使用未公开到 OpenAPI 的 `POST /v1/internal/conflict-retrieval`；它在完整四路
召回结果上保留最多 6 条同归一化标题制度证据，并对排除该制度族后的二级、一级及全库候选
统一 rerank，默认向 Agent 返回 10 条完整原文证据。
设计、实测和 Java 批量粒度见 `docs/conflict-agent-design.md`。

同一文件以 SHA-256 去重，重复上传返回既有 policy/document，并带 `reused: true`。
基础文件校验通过后，每次上传都生成独立的 `ingestion_run_id`；运行记录只保存
阶段、版本、计数和安全化错误，不保存制度正文或完整异常堆栈。

## 测试

普通测试不需要数据库：

```bash
uv run pytest -q
```

需要覆盖真实 PostgreSQL 事务时，额外设置 `PROOF_TEST_DATABASE_URL`。集成测试只创建
自己的临时制度，并在测试结束后按 policy ID 清理。

## 边界

本服务只拥有 `proofreading` 数据库中的 `proof_*` 表和 `.proof-data` 原文件存储。
能力目录只读挂载到 Framework，由 Framework 注册到自己的运行库；Proof 本身不直接写
`aituge` 数据库，也不改造旧 `services/proofreading`。
