# AI Framework 服务端口与上线验收

本文档以 `deploy/appliance/compose.yml` 为准，用于 `cortex-aio` 上的部署、候选版本验收和正式切换。默认从仓库根目录执行命令。

## 端口

| 服务 | 容器端口 | 默认宿主机监听 | 用途 | 外部暴露 |
| --- | ---: | --- | --- | --- |
| Framework | 8894 | `0.0.0.0:8894` | 智能问答、Agent、任务和审校流程 | 是 |
| Proof | 18100 | `0.0.0.0:18100` | 制度上传、审校数据、检索与安全 SQL | 是 |
| Smoke | 18200 | `127.0.0.1:18200` | 确定性能力挂载自检 | 否 |
| PostgreSQL | 5432 | `127.0.0.1:15432` | Proof 数据库 | 否 |
| Redis | 6379 | `127.0.0.1:16379` | Framework 运行状态 | 否 |

端口可通过 `deploy/appliance/.env` 中的 `FRAMEWORK_PORT`、`PROOF_PORT`、`SMOKE_PORT`、`POSTGRES_PORT` 和 `REDIS_PORT` 修改。正式环境不应把 Smoke、PostgreSQL 或 Redis 改为公网监听。

容器间使用服务名通信：Framework 访问 `http://proof:18100` 和 `http://smoke:18200`。`proofspace-network` 中的其他容器可访问 `http://ai-framework:8894` 和 `http://ai-proof:18100`。

## 主要入口

### Framework（默认 `http://HOST:8894`）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/` | 智能问答页面 |
| GET | `/openapi.json` | 服务健康及 API 描述 |
| GET | `/registry/capabilities` | 已挂载能力清单 |
| GET | `/single-agent/skill-packages` | Skill 包清单 |
| POST | `/single-agent/chat` | 单 Agent 问答，可选择 Proof Skill 包 |
| GET | `/scheduling/agents` | 调度 Agent 清单 |
| POST | `/scheduling/agents/{agent_id}/chat` | 指定 Agent 问答 |
| GET | `/task-manager/definitions` | 任务定义与审校能力清单 |
| POST | `/task-manager/tasks` | 创建任务 |
| POST | `/task-manager/tasks/{task_id}/run` | 执行任务 |
| POST | `/task-manager/runs/{run_id}/review` | 提交人工审校结论 |
| GET | `/task-manager/tasks/{task_id}/artifacts` | 查看任务产物 |
| GET | `/task-manager/artifacts/{artifact_id}/content` | 下载任务产物 |

### Proof（默认 `http://HOST:18100`）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/workbench` | 制度上传与审校工作台 |
| GET | `/dataset` | 数据集检查页面 |
| POST | `/v1/policies` | 上传并解析制度文件（multipart 字段 `file`） |
| GET | `/v1/policies` | 制度列表 |
| GET | `/v1/policies/{policy_id}/audit-status` | 审校状态 |
| GET | `/v1/policies/{policy_id}/semantic-findings` | 语义审校结果 |
| GET | `/v1/policies/{policy_id}/conflict-findings` | 制度冲突结果 |
| POST | `/v1/policies/{policy_id}/confirm` | 确认入库 |
| DELETE | `/v1/policies/{policy_id}` | 丢弃待审制度 |
| GET | `/v1/files` | 已上传文件清单 |
| GET | `/v1/files/{file_id}/content` | 在线读取原文件 |
| GET | `/v1/files/{file_id}/chunks` | 查看文件分块 |
| POST | `/v1/retrieval/search` | 语义、关键词或混合检索 |
| POST | `/v1/retrieval/fetch` | 按单元 ID 取原文证据 |
| POST | `/v1/query/sql` | 只读结构化 SQL 查询 |
| GET | `/v1/dataset/audit` | 数据集完整性检查 |

## 上线前验收

以下命令中的 `HOST` 可以是服务器域名、IP，或在服务器本机使用 `127.0.0.1`。候选版本应先绑定不同的临时端口；不得在验收前停止旧版本。

```bash
FRAMEWORK_URL=http://127.0.0.1:8894
PROOF_URL=http://127.0.0.1:18100
TENANT_ID=1

curl --fail --silent --show-error "$FRAMEWORK_URL/openapi.json" >/dev/null
curl --fail --silent --show-error "$PROOF_URL/health"
curl --fail --silent --show-error "$FRAMEWORK_URL/registry/capabilities"
curl --fail --silent --show-error "$FRAMEWORK_URL/single-agent/skill-packages"
curl --fail --silent --show-error -H "X-Tenant-ID: $TENANT_ID" "$PROOF_URL/v1/files"
curl --fail --silent --show-error -H "X-Tenant-ID: $TENANT_ID" "$PROOF_URL/v1/policies?limit=1"
```

仅当 `PROOF_DATASET_ROOT` 已配置时才执行数据集目录审计；未配置时该接口按契约返回 `503 dataset_unconfigured`，不代表应用健康检查失败。

```bash
curl --fail --silent --show-error \
  -H "X-Tenant-ID: $TENANT_ID" \
  "$PROOF_URL/v1/dataset/audit"
```

中文模糊 SQL 回归：

```bash
curl --fail --silent --show-error \
  -H "X-Tenant-ID: $TENANT_ID" \
  -H 'Content-Type: application/json' \
  -d '{"question":"包含金额的条款数量","sql":"SELECT count(*) AS count FROM policy_clause_corpus WHERE clause_text LIKE '\''%金额%'\''"}' \
  "$PROOF_URL/v1/query/sql"

curl --fail --silent --show-error \
  -H "X-Tenant-ID: $TENANT_ID" \
  -H 'Content-Type: application/json' \
  -d '{"question":"包含审批的条款数量","sql":"SELECT count(*) AS count FROM policy_clause_corpus WHERE clause_text ILIKE '\''%审批%'\''"}' \
  "$PROOF_URL/v1/query/sql"
```

文件上传应使用专门的验收文件，记录返回的 `policy_id` 和 `run_id`，依次检查解析、审校状态、文件列表、文件内容和分块。若该文件只用于验收，完成后通过对应 `policy_id` 删除，避免污染正式数据。

```bash
curl --fail --silent --show-error \
  -H "X-Tenant-ID: $TENANT_ID" \
  -F 'file=@services/proof/examples/采购管理制度（试行）.txt;type=text/plain' \
  -F 'title=候选版本上线验收' \
  -F 'version=smoke-test' \
  -F 'category_code=auto' \
  "$PROOF_URL/v1/policies"
```

至少确认以下项目全部通过后再切换正式端口：

1. Framework、Proof、Smoke 和依赖服务健康。
2. 能力清单同时包含 Proof 和 Smoke，Proof 的主 Skill 与 SQL 辅助 Skill 可见。
3. 文件可上传、解析、列出、在线读取和查看分块；配置了 `PROOF_DATASET_ROOT` 时，数据集目录审计也通过。
4. 审校状态及语义/冲突结果接口正常；启用模型审校时完成一次真实审校。
5. `LIKE '%金额%'`、`ILIKE '%审批%'`、普通 SQL 均返回 200。
6. 非只读 SQL、未授权表、超时查询和超出最大行数仍被拒绝或截断。
7. 智能问答分别完成一次原文证据题、结构化统计题和混合题。
8. 旧版本仍可回退；正式端口切换完成后再次执行本节全部检查。

只有候选版本和正式端口复验均通过，才停止并删除已明确记录名称和镜像摘要的旧应用容器。`runtime/` 持久化目录和数据库备份不属于旧容器清理范围。
