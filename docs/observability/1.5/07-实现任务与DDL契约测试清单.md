# 管理员观测平台实现任务、DDL 与契约测试清单

状态：待开发  
依据：已通过架构评审的 1.5 总体基线  
适用范围：Java 管理接口、Python Framework/Worker、管理员前端、网关与观测基础设施。

> 1.5 总体基线自此冻结。本文件承接实现决策、DDL 评审、代码任务和契约测试；完成本清单前不能声称管理员观测平台已经上线。

## 1. 实现前冻结的四项语义

### 1.1 模型 invocation 是“模型调度尝试”

logical_call_id 在业务决定调用模型时创建；invocation_id 只在即将进入本地执行器或外部 Provider 传输边界时创建。以下调用前失败不创建 invocation：

- 输入 Guardrail 拒绝；
- 凭据缺失；
- 模型路由或隐私策略拒绝；
- 请求 Schema、租户配额或业务前置校验失败。

这些失败写 Task Event；涉及安全边界时同时写 Security Event。输入 Guardrail 事件固定为 MODEL_INPUT_GUARDRAIL_REJECTED，不进入模型实际请求量、Provider 失败率、模型重试率或成本。

invocation 必须记录 dispatch_status：

| 状态 | 判断 | 统计口径 |
|---|---|---|
| NOT_DISPATCHED | 已进入调度边界，但能证明请求未进入 Provider 或本地执行器 | 不计实际请求量、Provider 失败率、模型重试次数和成本 |
| DISPATCHED | Provider 已接受/收到请求，或本地执行器已经开始 | 计入实际请求量；终态进入 Provider/业务成功率口径 |
| DISPATCH_UNKNOWN | 超时、断连或进程故障导致无法确认是否执行 | 单独统计，不静默归入成功、失败、已调度数量或确定成本 |

生命周期事件固定为：

- MODEL_INVOCATION_STARTED
- MODEL_INVOCATION_DISPATCHED
- MODEL_INVOCATION_DISPATCH_UNKNOWN
- MODEL_INVOCATION_SUCCEEDED
- MODEL_INVOCATION_FAILED
- MODEL_INVOCATION_VALIDATION_FAILED
- MODEL_INVOCATION_OUTPUT_GUARDRAIL_REJECTED
- MODEL_INVOCATION_OUTCOME_UNKNOWN
- MODEL_INVOCATION_ABANDONED

输出 Guardrail 发生在模型已经返回之后，因此保留 invocation，dispatch_status=DISPATCHED，计入 Provider 调用量、Token、延迟和成本，但业务尝试 outcome 不计成功。禁止继续使用含义不清的 MODEL_INVOCATION_GUARDRAIL_REJECTED。

模型指标分别提供 logical call 数、dispatch attempt 数、DISPATCHED 实际请求数、NOT_DISPATCHED 数、DISPATCH_UNKNOWN 数、逻辑调用成功率、已确认调度尝试成功率、基于已确认调度尝试的重试率和 active invocation 数。

### 1.2 跨租户审计按 Action 与 Access Session 去重

安全事件新增 audit_action_id、access_session_id、audit_layer 和 parent_audit_event_id。audit_layer 只能为 JAVA_GATEWAY 或 PYTHON_EXECUTION。

1. Java 为一次管理员意图创建唯一 audit_action_id，并写 JAVA_GATEWAY 根事件。
2. Java 调用多个 Python 接口时沿用该 ID；Python 子事件填写 PYTHON_EXECUTION 和 parent_audit_event_id。
3. “管理员行为次数”按 audit_action_id 去重；事件行数只用于执行层审计。
4. 新的跨租户列表授权创建短期签名 Access Session。Scope、权限、原因和筛选摘要不变时，前端复用 X-Observability-Access-Session。
5. Session 首次授权写安全事件；10 秒自动刷新只写访问日志和指标，不重复生成逻辑安全行为。
6. 敏感详情、导出创建/下载、权限和配置修改始终创建新的 audit_action_id。
7. Session 过期，或 Scope、筛选、用户、权限变化时必须重新授权。

### 1.3 能力型 Token 按敏感凭据处理

cursor、retryToken、runtime locator、Access Context、Access Session、highWatermark、可恢复查询的 querySnapshotId 和一次性下载 Token 都是能力型凭据。

- Java、Python、Nginx、网关、APM、Loki 和错误上报按字段名/Header 名屏蔽，不记录原值、前后缀或可重放哈希。
- 管理员普通响应统一返回 Cache-Control: no-store, private、Referrer-Policy: no-referrer、Vary: Authorization。
- Runtime 详情改为 POST /runtime-logs/detail，locator 放 JSON Body；旧 Path 形式不实现。
- cursor/retryToken 仍在 GET Query 时，代理日志按参数名替换为固定值 [REDACTED]，APM 不采集完整 URL。
- Java—Python highWatermark 只传短 hwm_... 句柄，并在双方访问日志中屏蔽。
- 前端只保存在页面内存，不进入 localStorage、sessionStorage、历史、埋点、错误上报或分享链接。
- 缓存、反向代理和 CDN 不得缓存管理员 API。

### 1.4 SSE 恢复缺口协议

- 409 EVENT_STREAM_REPLAY_GAP：Last-Event-ID 可识别，但客户端落后超过可重放窗口或缓冲溢出。前端先用分页 Run Event 补齐。
- 410 EVENT_STREAM_RESET_REQUIRED：Last-Event-ID 已清理、无法定位，或 Run 已归档。前端丢弃恢复游标，从分页接口第一页或当前任务详情重建；归档 Run 不再自动重连。
- 建连后才发现缺口时发送 reset-required SSE 事件，data 包含 code、runId、lastAvailableSequence，随后关闭连接。

部署约束：

- Cache-Control: no-cache, no-store, private
- X-Accel-Buffering: no
- 禁用代理响应缓冲和不适当的 SSE 压缩
- 每连接最大未确认积压为 1000 个事件或 5 MiB，先到者为准
- 客户端连续 30 秒无消费进展时发送 reset-required（若仍可写）并断开

## 2. DDL 评审任务

### 2.1 模型事件与投影

tuge_model_invocation_event 新增/确认 dispatch_status、pricing_version、cost_calculated_at；attempt_no 从 1 开始，Token、耗时、TTFT 和成本均非负。cost_currency 使用 ISO 4217。

约束：

- 输入 Guardrail、凭据缺失和路由拒绝不能插入 invocation 账本。
- 一个 invocation 最多一个 STARTED、一个 dispatch 结论和一个终态。
- DISPATCHED 才计入已确认实际请求；DISPATCH_UNKNOWN 独立展示。
- OUTPUT_GUARDRAIL_REJECTED 必须对应 DISPATCHED。
- 投影必须能从追加式事件完整重建。

tuge_model_invocation_projection 增加 dispatch_status、pricing_version、cost_calculated_at。索引至少覆盖：

- (tenant_id, started_at, invocation_id)
- (tenant_id, dispatch_status, started_at, invocation_id)
- (logical_call_id, attempt_no) UNIQUE

跨币种成本禁止直接相加。汇总返回按币种分组的 costs[]；本期不做汇率换算。以后若增加基准币种，必须持久化 fx_rate、fx_rate_source、pricing_version 和 cost_calculated_at，历史报表不能套用当前汇率或价格表。

### 2.2 安全审计事件

Java/Python 安全事件表增加：

- audit_action_id：NOT NULL
- access_session_id：可空
- audit_layer：NOT NULL
- parent_audit_event_id：可空

建议索引：

- (tenant_id, occurred_at, event_id)
- (audit_action_id, occurred_at, event_id)
- (access_session_id, occurred_at, event_id)
- (parent_audit_event_id)

audit_action_id 不做全表唯一：一次行为允许一条 Java 根事件和多条 Python 子事件。根事件按授权/写操作幂等，Python 子事件按 audit_action_id、service、action、request_id 或登记过的等价键幂等。

### 2.3 Java 业务事件因果版本

biz_business_event 新增 aggregate_version BIGINT NOT NULL CHECK >= 1。唯一约束优先使用 (tenant_id, aggregate_type, aggregate_id, aggregate_version)；若 aggregate_id 已全局唯一，可使用三列约束。

业务聚合表同时启用乐观锁版本。业务状态、版本事件和 Outbox 在同一事务提交；并发更新只能有一个版本成功，失败方重新读取后决定重试，不能生成重复或倒序事件。

## 3. 安全审计故障策略

| 操作 | 策略 | 最低要求 |
|---|---|---|
| 普通本租户元数据查询 | fail-open | 访问日志、Loki、指标失败不影响查询 |
| 新建跨租户 Access Session | fail-closed | 根安全事件或可靠 Outbox 无法落盘则拒绝授权 |
| 跨租户自动刷新 | Session 内不重复安全事件 | Session 有效且 Scope/权限/筛选未变化 |
| 敏感详情 | fail-closed | 审计无法可靠落盘则拒绝返回 |
| 跨租户导出创建与下载 | fail-closed | 状态、审计事件和 Outbox 按事务边界可靠提交 |
| 权限、模型凭据/路由、安全策略修改 | fail-closed | 状态变更与审计/Outbox 原子提交 |
| 审计清理、修复和重放 | fail-closed | 独立账号、审批上下文和不可跳过的审计 |
| 普通业务运行日志、Trace、指标 | fail-open | 观测旁路故障不改变业务结果 |

稳定错误码使用 SECURITY_AUDIT_WRITE_REQUIRED。Python 子事件若已有 Java 根事件和同事务 Outbox，可异步重试；不能重复管理员行为统计。

## 4. OpenAPI 实现约束

- /timeline 的 taskId、runId、requestId、traceId、reviewId 至少提供一个，否则返回 400 OBSERVABILITY_CORRELATION_REQUIRED。
- 统一事件主键统一为 eventId，不再混用 id。
- 外部 /model-summary 增加 modelName 筛选。
- Runtime locator 过期返回 410 RUNTIME_LOG_LOCATOR_EXPIRED；无权对象仍返回不可枚举的 404。
- /security-events/{eventId} 的 Python/聚合来源不可用时返回 503。
- 所有比例限制为 0 到 1；Token 数、耗时、延迟、计数、序号和成本非负，业务序号/版本从 1 开始。
- 普通管理员响应执行全局防缓存 Header；SSE 使用本文件 1.4 节的覆盖值。

## 5. 分层实现任务

### Java

1. 实现安全响应 Header 和能力型 Token 日志脱敏 Filter。
2. 实现 Access Session 签发、校验、复用、撤销和 audit_action_id 根事件。
3. Runtime 详情改为 POST Body locator，并区分 404/410。
4. 为 /timeline 实现至少一个关联键校验。
5. 高风险操作与安全审计/Outbox 同事务并 fail-closed。
6. biz_business_event 写 aggregate_version，业务表启用乐观锁。
7. SSE 代理实现 409/410、reset-required、积压和慢客户端策略。

### Python Framework/Worker

1. 将输入 Guardrail、凭据和路由前置失败移出 invocation 账本。
2. 实现 dispatch 状态机、输出 Guardrail 事件和可重建投影。
3. 模型摘要按 dispatch 状态统计，并按币种聚合成本。
4. Python 安全事件沿用 Java audit_action_id，写执行层和父事件关联。
5. 内部 SSE 支持 replay gap/reset required，并关闭代理缓冲。
6. highWatermark 和所有内部能力型 Token 在日志/APM 中脱敏。

### 管理员前端

1. Access Session/Context、cursor、retryToken 和 locator 只保存在内存。
2. 10 秒跨租户刷新复用 Session；Scope/筛选改变时申请新 Session。
3. 收到 409/410 或 reset-required 后转为分页恢复，不无限重连。
4. 模型页面分别展示调度尝试、已确认实际请求和调度未知；成本按币种分组。
5. 详情、复制和分享不得携带能力型 Token。

### 网关与平台

1. 屏蔽指定 Query、Header 和 JSON 字段；关闭 APM 完整 URL 采集。
2. 管理员 API 禁止缓存并设置 Referrer Policy。
3. SSE 禁用缓冲和不适当压缩，落实积压和超时上限。
4. 为脱敏规则、缓存 Header 和 SSE 配置建立部署后探针。

## 6. 必须执行的测试

- [ ] 输入 Guardrail、凭据缺失、路由拒绝不创建 invocation，模型实际请求计数不增加。
- [ ] NOT_DISPATCHED、DISPATCHED、DISPATCH_UNKNOWN 及崩溃恢复均有测试。
- [ ] 输出 Guardrail 保留 Provider Token/延迟/成本，但业务 outcome 不计成功。
- [ ] 多币种返回独立 costs[]，禁止把 CNY 与 USD 相加。
- [ ] 一次 Java 聚合请求调用多个 Python Path，只产生一个 audit_action_id 行为计数。
- [ ] 跨租户 10 秒刷新复用 Session，不递增逻辑安全行为；敏感详情仍新增行为。
- [ ] Session 过期、权限撤销、Scope 或筛选变化均拒绝复用。
- [ ] 所有能力型 Token 在 Java/Python/Nginx/APM/Loki 中均不可检索到原值。
- [ ] 管理员响应具有 no-store/private、no-referrer、Vary Authorization；SSE 具有 no-cache 和 X-Accel-Buffering=no。
- [ ] Runtime locator 不进入 URL；过期返回 410，无权对象返回 404。
- [ ] SSE 清理、积压溢出、Run 归档和慢客户端分别触发 409/410/reset-required 与分页恢复。
- [ ] /timeline 无关联 ID 返回 400，任意一个合法关联 ID 可查询。
- [ ] 并发更新只产生连续唯一 aggregate_version，事务回滚不留下事件或 Outbox。
- [ ] 审计故障时高风险操作 fail-closed；普通观测旁路故障不影响业务。
- [ ] OpenAPI 标准校验、Java/TypeScript/Python 代码生成和序列化烟测通过。

## 7. 验收边界

只有 DDL、迁移回滚、单元测试、并发测试、契约测试、生产构建和测试环境真实链路均通过后，才能把能力从“待开发”改为“已实现”。Mock SSE、Mock Provider、内存数据库或仅解析 YAML 的结果不能声称为真实部署验证。
