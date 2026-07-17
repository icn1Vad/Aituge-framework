# Proof 草稿审校与确认入库

## 流程

```text
上传文件
  -> 解析并把 Policy/Document/Chunks 暂存为 draft
  -> proof.audit.run 异步语义审校
  -> GET quality-report 查看合并报告
  -> 人工确认
  -> Policy 变为 effective，开始参与知识库检索
```

暂存是异步审校和页面刷新所需的技术状态，不代表正式入库。关键词、向量、Chunk fetch 和
Agent SQL 视图都只读取 `effective`。发现问题后，V1 在原文件中修改并重新上传，不在线修改
Chunk。

## Task 与输出

Proof 仍使用一个 Capability Registry：

- `proof.qa.chat`：对已生效制度进行问答；
- `proof.audit.run`：使用 `proof-policy-semantic-audit` 对草稿的全部 Chunk 分批审校。

每个 Chunk 最多返回一条 Finding，同一 Chunk 的多个问题合并：

```json
{
  "id": "retrieval_unit_id",
  "quote": "原文中的连续片段",
  "problem": "具体语义问题及影响",
  "suggestion": "可执行的修改建议"
}
```

无问题的 Chunk 不返回。Framework 强校验批次 JSON，并把完整批次结果回传 Proof。Proof 再
校验 Chunk ID、全量覆盖、每 Chunk 唯一性和 quote 原文定位；任一批次或校验失败，本次审校
整体失败且不保存部分 Finding。

## 最小数据

- `proof_audit_run`：每个 Document 一条当前运行记录，保存状态和 Framework task/run ID；
- `proof_audit_finding`：保存 Chunk ID、quote、problem、suggestion；
- 暂不保存文档快照、规则/Skill/模型版本、字符偏移、失败 Chunk 或历史轮次。

质量报告沿用 `/v1/policies/{policy_id}/quality-report`，版本为 `policy-quality-v2`。确定性结构
检查和语义 Finding 统一放在 `finding_counts` 与 `findings` 中。

## 状态接口

- `POST /v1/policies`：上传并暂存草稿，随后调度审校；
- `POST /v1/policies/{id}/semantic-audit`：重试失败审校；
- `POST /v1/policies/{id}/confirm`：审校完成后确认入库；
- `DELETE /v1/policies/{id}`：只允许丢弃草稿；
- `POST /v1/internal/semantic-audits/result`：Framework 固定可信回调。

当 `PROOF_SEMANTIC_AUDIT_ENABLED=false` 时不创建审校任务，用户查看确定性报告后即可确认。
