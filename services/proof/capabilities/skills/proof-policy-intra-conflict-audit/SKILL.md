---
name: proof-policy-intra-conflict-audit
description: Audit material rule conflicts between different Chunks of one draft policy.
---

# Proof 制度内 Chunk 冲突审校

## 目标

只判断当前 item 的一个 target Chunk 与同一制度中其他 Chunk 是否存在实质性规则冲突。
不要审查单个 Chunk 内部的自相矛盾，也不要比较其他制度。

## 必须执行

1. 读取 Current item.targets；该数组必须且只会有一个 target。
2. 使用 target 的 unit_id 调用 proof_intra_conflict_search，恰好一次。
3. 只比较工具返回的 source 与 results。results 已按纯向量余弦相似度返回最多 10 个同制度
   的其他 Chunk，并分别带有 C01 到 C10 的临时候选 ref；不得补充搜索，不得比较 source 自身。
4. 条件、适用对象、时间范围或例外条件不同且可以同时成立时，不得报告冲突。
5. 没有充分冲突证据时返回空 findings。

## 冲突类型

- numeric_conflict：同一事项在相同条件下规定了不相容的数值、期限、比例或阈值。
- authority_conflict：同一事项在相同条件下赋予了不相容的审批、决定或责任权限。
- process_conflict：同一事项规定了不能同时执行的流程、顺序或必经步骤。
- rule_reversal：相同条件下，一处要求或允许，另一处禁止；或一处必须，另一处明确无需。

## 输出

只返回严格 JSON：

    {
      "findings": [
        {
          "candidate_refs": ["C01"],
          "conflict_type": "numeric_conflict",
          "problem": "简明说明两处规则为何不能同时成立",
          "suggestion": "给出可执行的统一修改建议"
        }
      ]
    }

不要输出当前 target ID 或任何 Chunk ID。candidate_refs 必须逐字复制工具 results 中的 ref，
只能使用 C01 到 C10，不得为空、不得重复。不要输出 id、candidate_ids、scope、citation、quote、
evidence 或其他字段；真实 Chunk ID 由父服务根据当前 item 和 ref 安全映射。
同一组 Chunk 的同一冲突类型只输出一次；父服务还会对反向结果做最终归并。
