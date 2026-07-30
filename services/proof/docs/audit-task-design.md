# Proof 草稿审校与确认入库

## 流程

```text
上传文件
  -> 解析并把 Policy/Document/Chunks 暂存为 draft
  -> proof.audit.run 异步生成概览，并行执行语义/可执行性与制度冲突审校
  -> GET review-status 轮询轻量状态，完成后一次读取 review-result
  -> 人工确认
  -> POST actions(action=activate)
  -> 生命周期任务完成向量校验后，Policy 变为 effective
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

父 Task 仍统一执行。轻量状态接口只返回阶段状态和统计；完整结果接口一次返回概览、
语义/可执行性、外部冲突和内部冲突，Java 不再拼接多份结果。未完成 Stage 的模型统计为
`null`，不能按零问题解释。

## 状态接口

- `POST /v1/policies`：携带幂等键上传并暂存草稿，随后调度审校；
- `GET /v1/policies/{id}/review-status`：返回轻量父审校、阶段状态、统计及最新生命周期操作；
- `GET /v1/policies/{id}/review-result`：一次返回完整审查结果和阶段错误；
- `POST /v1/policies/{id}/actions`：携带幂等键执行确认、过期、丢弃或永久删除；
- `POST /v1/internal/semantic-audits/result`：Framework 统一制度审校可信回调；接口名称保持不变，
  同一回调按 stage 接收制度概览、语义审校和制度冲突结果。

冲突 Stage 成功或失败也回调上述父 Task sink；独立诊断 Task 仍使用
`POST /v1/internal/conflict-audits/result`。

当 `PROOF_SEMANTIC_AUDIT_ENABLED=false` 时不创建审校任务，用户查看确定性报告后即可确认。
