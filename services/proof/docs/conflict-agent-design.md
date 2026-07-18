# Proof 制度冲突 Agent 方案与实测结论

## 结论

生产主链应采用：

```text
一个文档级 Conflict Audit Task
  -> 确定性并行预取每个 source unit 的冲突 RAG
  -> 同归一化标题制度族向量 Top6 保底
  -> 排除同制度族后，二级 Top6 + 一级 Top4 + 全库 Top2 统一 rerank
  -> 给 Judge 展示默认 10 条证据
  -> 每批 1～2 个 source，单次联合 LLM Judge
  -> 证据/引用强校验、Finding 归并
  -> Task 汇总后返回 Java
```

不要把数值、权责、流程、规则反转拆成四套 RAG；它们是统一 Judge 的输出维度。也不要让
ReAct 自由决定是否搜索、搜索几轮。普通批审中 source unit 已知，工具调用是确定动作，先由
Task 编排器执行比让模型选择工具更快、更便宜、更稳定。

保留一个受限 ReAct Agent 作为复杂组合冲突的调查/回退路径：每个 target 只允许一次
`proof_conflict_search`，不得自由改写 query；仅在固定 Judge 返回证据不足或需要补充多规则组合时
使用。当前已部署 `proof-conflict-agent` 和 `proof.conflict.audit` 用于验证该路径。

## 为什么这样设计

- [ProvBench](https://aclanthology.org/2025.acl-long.312/) 将合同自动审查拆为“推荐相关法律条文”
  和“判断条款与法条是否冲突”，与 Proof 的 RAG + Judge 边界一致。
- [ContractNLI](https://aclanthology.org/2021.findings-emnlp.164/) 要求同时输出蕴含/矛盾/未提及
  标签和证据 span，并指出例外中的否定是难点；因此不能只做关键词反义匹配。
- [DocInfer](https://aclanthology.org/2022.emnlp-main.51/) 表明长文档推理需要先选出分散证据再联合
  判断，支持“RAG 证据选择”和“多规则联合 Judge”分层。
- [LEXDEMOD](https://aclanthology.org/2022.emnlp-main.795/) 将法律规范表达建模为与主体关联的义务、
  许可、禁止等模态，支持权责/规则反转判断必须比较主体和规范效力，而不是只识别“必须/不得”。
- [ReAct](https://arxiv.org/abs/2210.03629) 的价值是需要根据观察决定下一动作的交互任务。Proof
  普通批审的首次检索动作已经确定，所以完全 ReAct 会增加一次模型轮次；只在证据不足时才有价值。
- [CRAG](https://arxiv.org/abs/2401.15884) 强调先评价检索质量再触发纠正动作。Proof 的分支计数、
  降级原因和证据覆盖率可以作为是否进入 ReAct 回退的门槛。
- OPA 的官方冲突说明同样把问题定义为：共同条件可能成立时，同一个决策产生多个不同输出；修复
  方式是让条件互斥或显式定义优先级。参见
  [complete rule conflict](https://www.openpolicyagent.org/docs/errors/eval-conflict-error/complete-rules-must-not-produce-multiple-outputs)。

## 统一 Judge 判定

所有候选依次通过三道门：

1. **同一事项**：约束的是同一业务对象和同一决策槽位。
2. **适用范围重叠**：主体、条件、金额、时间版本、地区、组织层级存在共同可发生区间。
3. **不可同时满足**：执行一条必然违反另一条，或多条组合后产生两个无法确定优先级的唯一结果。

四类输出：

- `numeric_conflict`：同一阈值、期限、比例、频率、公式或区间边界不一致；先统一单位、期限起点、
  单笔/累计和区间开闭。
- `authority_conflict`：同一唯一责任、审核、批准、决定、否决或授权边界给了不相容主体。
- `process_conflict`：前置条件、先后顺序、渠道、材料、升级链互斥、断裂或循环。
- `rule_reversal`：同一行为在共同条件下同时被要求、许可、禁止或豁免。

一次冲突可能同时含数值和流程维度，输出保留一个主类，其他维度合并写入 `problem`；三条以上
共同造成冲突时，将相关 Chunk ID 放入同一条 `candidate_ids`，不另设机制字段。

## RAG 到 Judge 的工程修正

真实 Qwen3 reranker 会把部分金标推到第 8～12 名，纯 reranker 可能丢失关键证据。当前实现为：

1. 对 `normalized_title` 相同的制度族向量召回 Top6 并直接保留；
2. 二级分类、一级分类和全库分支都排除当前制度及同归一化标题制度；
3. 三路分别召回 Top6、Top4、Top2，按 `text_hash` 去重后一次 rerank；
4. 先返回同制度族证据，再用 reranker 候选补足默认 10 条；
5. Agent 视图仅精简字段，不再二次排序。

实测 35 个 PT 跨制度场景：任一证据 Recall@3 100%，全部证据 Recall@5 97.1%、
Recall@10 100%。报告见
`reports/conflict-method-eval/conflict-agent-view-top10-title6-nontitle-rerank-benchmark.json`。当前 PT 金标全部来自
原始标题完全相同的制度，因此该报告只能证明同制度族召回，不能外推为不同名制度冲突召回率。

## Agent/固定编排实测

以 PT-014 的 100 万/150 万担保门槛冲突为例：

| 方法 | 结果 | Tokens | 耗时 | 观察 |
| --- | --- | ---: | ---: | --- |
| ReAct，原 rerank 顺序 | 漏掉金标数值冲突 | 18,728 | 约 90s | 金标在第 11/12，注意力被前部噪声占用 |
| ReAct，同名优先排序 | 命中数值冲突 | 16,683 | 约 60s | 正确，但成本和时延仍高 |
| 固定预取 + 单次 Judge，思考开启 | 命中 | 13,179 | 177s | 7,873 reasoning tokens，不适合批审 |
| 固定预取 + 单次 Judge，思考关闭 | 命中并合并为流程主类 | 4,637 | 18.6s | 比 ReAct 少约 72% tokens |

复杂 PT-001 上，固定单次 Judge（思考关闭）使用 7,092 tokens、约 60s，返回 5 条联合/内部冲突；
说明复杂度仍会影响 completion 长度。因此 Skill 现限制每个 target 最多 3 条 Finding、每批最多 2 个
source，并要求合并同事项问题。

ReAct 还暴露了三类工程故障：长 ID 抄错导致工具 404、候选过多导致漏检、长输出被截断导致 JSON
解析失败。当前分别通过“唯一单字符 ID 安全纠错”“同制度族保底 + Top10 Judge 视图”“输出数量上限 +
Task 重试”缓解；但这些实测仍支持固定编排作为默认主链。

## 同制度内部冲突

当前跨制度 RAG 有意排除当前制度，避免自引用。source 单条内部的自相矛盾由 Judge 直接检查；
不同 sibling 条款之间的冲突不能依赖跨制度 RAG。完整文档 Task 必须在外层增加同制度局部候选：

- 同一章节/相邻条款窗口；
- 同一事项的文档内向量候选；
- 每批最多两个 source，让 Agent 同时看到双方 canonical source。

这一路应由 Task 编排器确定性形成，不加入跨制度 RAG 的四路结果，也不让 Java 拆成多个 Task。

## Java 与批量成本

Java 对一份制度只创建整体 `proof.audit.run` 父 Task，不单独创建冲突 Task。父 Task 的
`conflict_audit` Stage 每个 item 放 1 个 source，Framework 以 `max_concurrency=4` 并行执行，
最终随整体质量报告返回；独立 `proof.conflict.audit` 只用于测试和诊断。

- 不要“一条制度一个 Java Task”，否则幂等、状态、回调和汇总都碎片化。
- 不要“整份制度所有条款一次塞进一个 Judge”，否则候选数按 source 成倍增长，漏检和截断风险上升。
- 省钱的粒度是：一个父 Task、多个小 item、每个 source 一次 embedding/RAG、每 item 一次 Judge。
- 相同 source 的 RAG 结果应按 `(unit_id, policy corpus version, retrieval config version)` 缓存；模型失败
  重试时复用证据，不重复 embedding/rerank。

当前 Framework Task 已提供进度事件和最终聚合结果；Proof 回调只校验 target ID、candidate ID、
当前文档归属和候选有效状态。模型不再复制 quote/citation，原文展示按 Chunk ID 从数据库读取。
