# Page 2 Python 模型调用可观测性交接

## 范围与边界

- 分支：`obs/20-python-model`
- 基线：`f24bdb6fb51a9002db5e7e58dcfd7f16106968e5`
- 仅修改 Python 模型调用、事件账本、查询/快照、reconciliation、Proof 模型边界及相关测试。
- 未修改冻结的 `docs/observability/1.5/01`—`07`、README、两份 OpenAPI、Java、前端或正式环境。

## 已交付

- PostgreSQL 追加式模型调用事件、投影、服务端因果序列、全局有界快照与 001/002 可逆迁移。
- 事件高水位线中 `maxSequence` 与 `maxEventId` 来自同一因果元组；`maxIngestedAt` 独立取最大入库时间。
- orphan reconciliation 对无权威租约证据的调用只落 `OUTCOME_UNKNOWN`；不会伪造独立的 `DISPATCH_UNKNOWN` 事实，也不会猜测 `ABANDONED`。
- LLM Runtime、ReAct、输出 guardrail、标题、Task Memory、Task Pipeline、合同 Revision Draft 与 Proof embedding/reranker/离线模型工具的终态闭环。
- deferred finalizer 在取消、生成器异常、session 关闭异常和业务校验失败时均可终态化；身份冲突集合统一为 duplicate、identity、invalid transition、logical-call conflict 并 fail closed。
- Revision finalize capability token 绑定 tenant、invocation、logical call、attempt、用量、provider request id、真实 provider finish reason 与严格到期边界；finish reason 经过白名单、长度和控制字符校验并进入成功终态 metadata。
- 机器清单覆盖 Page 2 已接线的真实 Provider 边界；合同 Revision Gateway 明确分类为内部观测代理而非 Provider。
- 固定列与 `pricing_version` 在落库前拒绝 JWT、跨页 `hwm_` / `cur_` / `crf_revfin_v1.` / `qtk_qs_` / `cap.` / `obs1.` capability，以及高置信 API key、Bearer、Authorization 等凭据形态；exact 与 embedded 形态同样拒绝。
- 普通列表筛选与 detail `invocationId` 在查询哈希、快照和数据库访问前执行同一敏感值检查，避免 capability 进入代理 URL、访问日志或持久查询。
- 四个模型操作使用冻结 operationId；框架参数校验在本路由运行时归一化为扁平 400 `OBSERVABILITY_QUERY_INVALID`，不返回 FastAPI `detail` / `input`。
- `X-Request-ID` 只有在格式、长度和敏感值检查都通过后才进入 Authorizer 或响应；否则生成新 `request_...`，响应体和响应头都不回显原值。
- deadline、retryable 503、`no-store`、`no-referrer`、`Vary` 与 Scope Token 不回显边界保持不变。
- 错误响应只允许本页显式注册、满足稳定格式且通过敏感标识检查的 code；`HTTPException.detail.code` 与领域异常 `.code` 的未知、畸形或敏感值均按 HTTP status 回退，不进入 body、header 或序列化响应。
- `SingleAgentRunner` 在构造 agent 前检查可选 observability 参数的真实签名：生产 `ReactAgent` 仍绑定共享 `LlmRuntime` / `model_id`，旧注入 agent 不再因新增关键字失败，且不会吞掉构造器内部 `TypeError`。
- discussion 的 speak/pass、系统指令、内部 runtime context 与 SSE chunk 既有断言保持不变；combined framework 的 `FakeLlmRuntime` 已补齐当前 `astream` 边界。

## 机器契约

- `backend/model_observability/model_metadata_registry.v1.json`
  - metadata key 使用 snake_case、按事件类型白名单、对象上限 1024 bytes。
  - `finish_reason` 的 Provider 白名单为 `stop`、`length`、`tool_calls`、`content_filter`、`function_call`；`provider_completed` 仅为已落库 v1 数据的兼容枚举。
- `backend/model_observability/provider_call_inventory.v1.json`
  - 声明已收窄为 Page 2 自有模型边界 AST 清单，不声称是仓库全量 HTTP 清单。
  - Proof 的全部直接 `httpx.post` 模型边界均由 AST 扫描与清单双向核对。
- Revision capability token 稳定前缀：`crf_revfin_v1.`。

## 跨页面交接

### Page 4

- 按 metadata registry 的精确 snake_case key 与事件范围消费；不要把 `attemptNo`、`fallbackFromInvocationId`、`featureCode`、`timeToFirstTokenMs` 当 metadata。
- 查询水位线必须保持：`maxSequence` / `maxEventId` 同一因果元组，`maxIngestedAt` 独立最大值。
- 全局快照容量默认上限为 1000，部署前需由容量责任人确认配置，不应把该默认值冻结成外部 API 契约。

### Page 5

- 日志、trace、错误、导出与 SSE 必须把前缀 `crf_revfin_v1.` 及其后完整 token 作为 capability secret 脱敏；不得依赖对象 `repr` 保护作为唯一防线。
- finalize 身份冲突必须 fail closed，不得将 duplicate、identity、invalid transition 或 logical-call conflict 降级成普通 telemetry 告警。

### Page 6 / Page 8

- 集成前执行 `001_model_invocation_ledger.up.sql`、`002_model_observability_hardening.up.sql`，并保留 checksum；共享迁移入口由 Page 8 接线。
- Framework 运行时需显式启用 `MODEL_INVOCATION_LEDGER_ENABLED`；Proof 使用 `PROOF_MODEL_OBSERVABILITY_DATABASE_URL`、tenant/offline run/service version 配置。未配置时保持 fail-open telemetry，不得静默声称已覆盖。
- 本页路由已把实际框架校验失败转换为 400；FastAPI 生成的 OpenAPI 仍自动声明 422。Page 8 必须做根 schema 后处理并对冻结 OpenAPI 精确断言。
- Authorizer 仍需二次校验 method、path、query hash、request ID、环境、权限和租户；不得记录被替换前的 request ID 或其他凭据。
- Revision complete/finalize 是内部代理链路，Provider 事实由 Framework `LlmRuntime` 所有；不要在 Contract 代理层重复创建 Provider invocation。

## RED（不得误报为已闭合）

1. **ABANDONED 租约权威缺失**：当前 Python 侧没有可证明 active/expired 的 worker lease/fencing source，因此 reconciliation 保守落 UNKNOWN。Page 1/3/8 接入权威租约与 fence 前，不得发出 `MODEL_INVOCATION_ABANDONED`，也没有 active-vs-expired lease race 的诚实验收证据。
2. **Legacy Provider 边界未接线**：`OpenAILike` 与 `OpenAICompatibleReranker` 当前无生产构造路径，清单标为 `UNWIRED_RED`。若后续启用，必须先接入生命周期 recorder。
3. **部署接线未执行**：本页面未修改共享入口、Compose、活动发布目录或正式环境；迁移执行、环境变量和路由/依赖合并由 Page 8 完成。
4. **冻结 OpenAPI 尚未精确收敛**：测试镜像在基线也有 FastAPI/Pydantic multipart 表达漂移；四个 operationId 和运行时 400 已收敛，但生成 schema 仍含框架自动 422，必须由 Page 8 根级处理并复验。

## 验证证据

- 本次受影响行为族：`64 passed, 1 deselected`；唯一 deselect 是单独留给全仓对照的 Redis live-history 用例。
- 当前 Page 2 相关宽回归：`293 passed, 14 skipped`；14 项均为未提供 PostgreSQL URL 时显式跳过的集成测试。
- 当前全仓 `pytest -q tests backend/single-agent/tests`：`2 failed, 652 passed, 60 skipped`。两个失败均为环境外部依赖：
  - `tests/test_live_multi_capability.py::test_live_model_calls_tool_from_second_capability_entry`：network-none 下访问 `127.0.0.1:8001/health`，`ConnectError: [Errno 111] Connection refused`。
  - `backend/single-agent/tests/test_single_agent_api.py::test_conversation_manager_compresses_large_redis_history`：`localhost:6379` 的 Redis 不可用，history restore 返回空。
- 同镜像、同 network-none 条件下的 `f24bdb6` 基线：`3 failed, 481 passed, 46 skipped`；包含上述相同 live/Redis 两项，以及已被本页既有实现修复的 `test_absence_finding_is_unsupported_without_model_call`。因此本次最初出现的 15 项新增失败均已闭合，没有相对基线新增的剩余失败：
  - 10 项旧 `ReactAgent` test double 是新增可选构造参数造成的生产兼容回归，由签名协商修复。
  - 4 项 discussion 是同一构造异常被业务层降级为 pass，修复后原 speak/pass、system prompt、runtime context、SSE assertions 原样通过。
  - 1 项 combined flow 是模拟 `LlmRuntime` 未实现当前 `astream` 接口，已按生产边界补齐 test double。
- 新增 runner 回归同时证明真实 `ReactAgent` 获得 runtime/model identity、legacy 构造路径可用、构造器内部 `TypeError` 不会被误吞。
- error-code 负向矩阵对 HTTP detail 与领域异常两种来源逐一覆盖 qtk、JWT、hwm、cur、crf、cap、obs1、sk、Bearer、Authorization、api_key、secret、畸形 code 和格式正确但未注册的 `UNREGISTERED_UPPERCASE_CODE`；逐项断言 body、header、完整 serialized response 均无原值。
- `git diff --check` 与变更文件 `python -m compileall` 通过。
- 本次修复未修改 migration、repository、snapshot 或 PostgreSQL 逻辑。前序提交 `979e140` 已有真实 PostgreSQL `14 passed` 证据，覆盖并发幂等、因果序列、反序 ingested watermark、全局快照容量/并发、reconciliation late-worker fence、迁移 checksum/rebuild。
- 前序提交 `979e140` 的显式 `psql -v ON_ERROR_STOP=1` 非空 `001 -> 002 up -> 002 down -> 002 up`：
  - 首次 up：event/projection 各 1 行，`server_sequence` / `started_sequence` 非空。
  - down：event/projection 各 1 行，`server_sequence` 列数为 0。
  - 再次 up：event/projection 各 1 行，唯一 sequence 为 1，`started_sequence=1`。
  - 隔离 schema 清理后剩余数为 0。
- 本次尝试复跑 PostgreSQL 时，精确检查的 `postgres:14-alpine`、`postgres:14`、`postgres:15-alpine`、`postgres:16-alpine`、`postgres:16` 均为 `No such image`；拉取 `postgres:14-alpine` 因 Docker daemon 代理 `127.0.0.1:7897` 拒绝连接而失败。未查询或复用任何运行中数据库，也未据此误报新 PG 证据。
- detached baseline worktree、pytest root-owned cache及全部本次 `contract-review-test-page2-*` 容器均已精确删除；没有留下运行状态。后续资源名必须明确包含 `contract-review-test` 或 `contract-review-dev`。
