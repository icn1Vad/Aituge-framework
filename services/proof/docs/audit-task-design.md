# Proof 草稿审校与确认入库

## 流程

```text
上传文件
  -> 解析并把 Policy/Document/Chunks 暂存为 draft
  -> proof.audit.run 异步生成概览，并行执行语义/可执行性与制度冲突审校
  -> GET audit-status 轮询轻量状态，按 Stage 分别获取结果
  -> 人工确认
  -> Policy 变为 effective，开始参与知识库检索
```

暂存是异步审校和页面刷新所需的技术状态，不代表正式入库。关键词、向量、Chunk fetch 和
Agent SQL 视图都只读取 `effective`。发现问题后，V1 在原文件中修改并重新上传，不在线修改
Chunk。

## Task 与输出

Proof 仍使用一个 Capability Registry：

- `proof.qa.chat`：对已生效制度进行问答；
- `proof.audit.run`：唯一生产入口。概览、语义/可执行性、制度冲突三个 Stage 并行执行，
  `finalize_report` 确定性合并 Artifact；
- `proof.conflict.audit`：保留为冲突能力测试和诊断入口，上传流程不会额外创建它。

```text
proof.audit.run
├── policy_summary   -> proof_policy_summary
├── semantic_audit  -> proof_semantic_audit
├── conflict_audit  -> proof_conflict_audit
└── finalize_report (depends on all three) -> proof_audit_result
```

语义 Stage 按字符预算分批；冲突 Stage 每个 item 固定一个源 Chunk，并复用
`proof-conflict-agent`、`proof-policy-conflict-audit-package` 和 `proof_conflict_search`。

每个 Chunk 最多返回一条 Finding，同一 Chunk 的多个问题合并：

```json
{
  "id": "retrieval_unit_id",
  "category": "semantic_ambiguity",
  "problem": "语义歧义或可执行性缺口及其影响",
  "suggestion": "可执行的修改建议"
}
```

无问题的 Chunk 不返回。Framework 强校验批次 JSON，并把完整批次结果回传 Proof。Proof 再
校验 Chunk ID、全量覆盖和每 Chunk 唯一性；任一批次或校验失败，本次审校
整体失败且不保存部分 Finding。

## 最小数据

- `proof_audit_run`：每个 Document 一条当前运行记录，保存状态和 Framework task/run ID；
- `proof_audit_finding`：保存 Chunk ID、category、problem、suggestion；
- `proof_conflict_audit_finding`：保存源/候选 Chunk ID、四类冲突、problem、suggestion；
- 暂不保存文档快照、规则/Skill/模型版本、字符偏移、失败 Chunk 或历史轮次。

父 Task 仍统一执行，但读取接口按 Stage 拆分。状态轮询统一返回各类统计，但不返回概览正文或
Finding；某一 Stage 完成后，Java 或前端只请求该 Stage 的结果并透传其结构。未完成 Stage 的
模型统计为 `null`，不能按零问题解释。

## 状态接口

- `POST /v1/policies`：上传并暂存草稿，随后调度审校，返回轻量 `audit_task`；
- `GET /v1/policies/{id}/audit-status`：返回父审校、三个 Stage 状态及统一统计；
- `GET /v1/policies/{id}/policy-summary`：返回概览 Stage 结果；
- `GET /v1/policies/{id}/semantic-findings`：返回语义/可执行性及结构检查结果；
- `GET /v1/policies/{id}/conflict-findings`：返回冲突 Stage 结果；
- `POST /v1/policies/{id}/confirm`：审校完成后确认入库；
- `DELETE /v1/policies/{id}`：只允许丢弃草稿；
- `POST /v1/internal/semantic-audits/result`：Framework 统一制度审校可信回调；接口名称保持不变，
  同一回调按 stage 接收制度概览、语义审校和制度冲突结果。

冲突 Stage 成功或失败也回调上述父 Task sink；独立诊断 Task 仍使用
`POST /v1/internal/conflict-audits/result`。

当 `PROOF_SEMANTIC_AUDIT_ENABLED=false` 时不创建审校任务，用户查看确定性报告后即可确认。
