# 合同报告与问答共用能力实施记录

## 目标

复用 Aituge Framework 已有 ReAct Agent、Task、Run、Skill 和 HTTP Tool，建设合同报告与问答的
共用生成能力。本能力不重新执行合同审查，也不修改合同审查一期冻结协议。

## 固定链路

```text
已完成的合同审查结果
→ Framework ReAct Agent
→ REPORT / CHAT 业务 Skill
→ Markdown 内容 + Evidence ID
→ 确定性 Citation Finalizer
→ Markdown + references[]
```

模型只选择已经存在的 `evidence_id` 和链接文案。`block_id`、`chunk_id`、字符区间、原文及
SHA-256 全部由 Finalizer 从正式审查结果中补齐。

Markdown 引用格式固定为：

```text
[显示文字](#docref-EVIDENCE_ID)
```

前端后续可以将该链接渲染为“小飞机”，通过 `references[]` 中的定位字段复用 ONLYOFFICE
定位能力。

## 阶段 1：报告模式

- Task：`contract.grounded.answer`
- Pipeline：`contract-grounded-answer-pipeline-v1`
- Agent：`contract-grounded-answer-v1`
- Skill：`contract-grounded-answer`
- 内部工具：`contract_get_review_result`
- 当前模式：`REPORT`

报告草稿由 Agent 生成，引用由确定性 Finalizer 校验并物化。ABSENCE 证据可以作为普通文字
描述，但不得生成可点击引用。

## 阶段 2：问答模式

相同 Task、Agent、工具和引用协议现已支持 `CHAT` 模式：

- `question`为必填的当前问题；
- `conversation_history`最多20条，只用于理解上下文；
- 历史消息不是事实来源；
- 回答依据仍只有正式审查结果；
- 无法从结果确认时明确说明无法确定；
- 输出继续使用相同Markdown引用和`references[]`。

REPORT 与 CHAT 使用不同业务指令，但共用 Evidence 引用和定位数据结构。

## 阶段 3：问答流式输出

同步接口继续保留，新增：

```text
POST /v1/contract-reviews/{review_id}/chat/stream
Content-Type: text/event-stream
```

流式接口仍执行同一个`contract.grounded.answer` ReAct Task，不创建第二套问答 Agent。Contract
Python订阅Framework Run事件，只把结构化模型输出中的`content_markdown`增量转成`delta`，
不向Java暴露半截JSON。事件类型固定为：

```text
meta      任务、请求和合同标识
tool      Framework工具调用状态
delta     仅包含可展示的Markdown增量
snapshot  幂等复用已完成任务时返回完整结果
done      最终GroundedAnswerData
error     流开始后的标准错误
```

`delta`只能用于即时展示，不代表引用已经可用。`references[]`仍须经过Framework Citation
Finalizer，并由Java再次对照正式Finding和Evidence验证；只有`done`通过后，前端才可激活
`#docref-*`小飞机定位。
