---
name: proof-policy-conflict-audit
description: Audit policy units for evidence-grounded numeric, authority, process, and rule-reversal conflicts by using Proof conflict RAG and a joint multi-rule judge.
---

# Proof Policy Conflict Audit

审校 `Current item.targets` 中的制度规则是否与自身、同批规则或 `proof_conflict_search`
返回的历史有效制度规则冲突。目标是找出在同一事项、重叠适用范围内无法同时满足的规则，
不是罗列表述差异。

## 固定工作流

1. 第一轮必须为每个 target 的 `unit_id` 各调用一次 `proof_conflict_search`，`top_k=10`。
   同一轮并行发出全部调用；不得改写 query，不得重复调用同一 unit，不得调用其他工具。
2. 读取每个工具结果中的 `source`、`results`、`retrieval_sources` 和降级信息。
   target 自身文本与工具返回的 `source.text` 不一致时，以工具返回为准。
   所有后续 ID 必须复制 `source.id`/`results[].id`；若 `source.unit_id_corrected=true`，不得继续
   使用请求时误抄的 ID。
3. 先检查 source 单条规则内部是否自相矛盾，再联合比较同批 source 和全部候选。允许检查
   2 至 N 条规则共同造成的冲突，不能只做两两 NLI。
   工具已按 Judge 使用顺序返回证据；必须依次检查全部 `results`，不得只判断第一条。
4. 只对通过“同一事项 → 适用范围可同时成立 → 约束不可同时满足”三道门的证据返回 Finding。
5. 一次性返回严格 JSON。不得在最终 JSON 前后输出解释、Markdown 或思考过程。

每个 target 最多返回 3 条最重要的 Finding；更多问题必须按同一事项合并。将同一组 Chunk
之间无法同时执行的原因合并写入 `problem`，并给出一条 `suggestion`。

`proof_conflict_search` 保留最多 6 条同归一化标题制度证据，并对排除该制度族后的二级分类、
一级分类和全库候选统一 rerank。工具向 Judge 返回默认 10 条完整原文证据。
不要根据冲突类型再次分类检索；冲突类型是 Judge 的输出标签，不是召回路由。

## 冲突成立条件

### 1. 同一事项

比较的规则必须约束相同或可映射到同一个业务决策槽位，例如同一类报销的报送期限、同一
金额区间的审批主体、同一合同签署前的必经节点。只因都出现“审批”“管理”等词不算同一事项。

### 2. 适用范围重叠

明确比较主体、对象、组织层级、金额区间、时间版本、地区、业务类型、前提和例外。条件互斥、
不同流程阶段、不同金额区间或不同适用主体的规则可以并存，不报冲突。

### 3. 无法同时满足

在至少一个真实可发生的共同适用情形中，执行一条规则必然违反另一条，或者多条规则组合后
产生两个不同且无法确定优先级的唯一结果。仅仅更严格、更详细、增加备案或补充一个后续动作，
仍可同时完成时不报冲突。

显式的上位规则、特别规定、授权例外或新版本替代关系可以消解表面差异；但下位制度违反上位
强制规则、超越授权、擅自降低标准或把唯一权限交给另一主体时仍属于冲突。

## 四类输出

### `numeric_conflict`

同一语义槽位的阈值、期限、比例、频率、数量、计算公式或区间边界不一致。必须先统一单位、
期限起点、单笔/累计口径和开闭区间。两个不同数值分别用于预审和终审、上限和下限时不是冲突。

### `authority_conflict`

同一事项的责任主体、审核人、批准人、决定权、否决权、授权边界或职责分离要求不相容。
多个部门依次审核、会签或承担不同角色时不是冲突；只有规则都声称同一唯一角色或互斥权限时才报。

### `process_conflict`

同一事项的先后顺序、前置条件、办理渠道、材料路径或升级链无法同时执行，包括审批链断裂、
循环依赖和多条规则组合后路径不唯一。流程可顺序拼接或一条只是另一条的细化时不报。

### `rule_reversal`

同一行为在重叠条件下同时被要求、允许、禁止或豁免，导致规则方向反转。比较的是实际规范效力，
不能只匹配“必须/可以/不得”等触发词；合法例外、不同主体的权利义务和不同条件下的许可禁止
不是冲突。

若同一证据同时落入多类，选择最直接导致无法执行的主类。只有存在彼此独立的两个冲突时才返回
两条 Finding。三条或以上共同造成冲突时把所有相关 Chunk ID 放进同一条 `candidate_ids`，不要把
联合冲突拆成多个并不成立的两两冲突。

## 抑制误报与 ID 规则

- 相似不等于冲突，制度更新、措辞差异、职责协作、流程补充和标准从严都要单独验证。
- 无法证明共同适用或无法同时满足时返回无 Finding，不以“可能存在冲突”凑数。
- 不从模型记忆补法规、组织关系、版本优先级或隐含流程。只使用 target 和工具证据。
- `id` 必须等于被审 target 的 `id`；`candidate_ids` 写入造成冲突的其他 unit ID。若 source
  单条内部自冲突暂不返回 Finding；当前格式要求至少一个不同的候选 Chunk ID。
- 所有 ID 只能复制工具返回的真实 Chunk ID，不得使用 policy_id、document_id 或自行生成的编号。

## 输出

只返回一个合法 JSON 对象：

```json
{
  "findings": [
    {
      "id": "source-unit-id",
      "candidate_ids": ["conflicting-unit-id"],
      "conflict_type": "numeric_conflict",
      "problem": "说明两个 Chunk 为什么约束同一事项且无法同时执行。",
      "suggestion": "明确一个可落实的统一规则、优先级或适用边界。"
    }
  ]
}
```

枚举值必须严格使用：

- `conflict_type`: `numeric_conflict`、`authority_conflict`、`process_conflict`、`rule_reversal`
没有充分证据时只返回 `{"findings": []}`。
