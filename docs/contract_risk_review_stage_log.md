# 合同 Risk Review Playbook 化阶段记录

本文记录合同审查 Risk Review 层阶段 6 的实施状态、基线、验证结果和进入下一阶段的门禁。唯一架构设计依据为同目录下的 `contract_risk_review_design.md`；Java–Python–Framework 对外协议仍以唯一联合冻结稿为准。

## 工作约束

- 阶段 6 从阶段 5.4 提交 `b471c3624956899706c9aca7d379340cad17e6af` 建立独立工作树。
- 分支：`feat/contract-risk-review-playbook-v1`。
- 工作树：`D:\contract-risk-review-v1`。
- 不修改、测试、提交或覆盖原 Window 工作树中正在进行的改动。
- 每个实施阶段开始前重新阅读联合冻结稿、阶段 6 设计稿和本记录。
- 每个阶段必须先完成本阶段测试、记录结果并推送远程功能分支，再进入下一阶段。
- 未经用户明确确认，不合并 `main`、不部署正式环境。

## 阶段 6.0：设计冻结

状态：通过。

### 基线

- 阶段 5.4 提交：`b471c3624956899706c9aca7d379340cad17e6af`。
- 阶段 5.4 内容：定向纠正合同 Window 随机改写；真实模型定向验证和完整 12 Window 链路均通过。
- 新分支直接从该提交建立，不包含原 Window 工作树中的未提交改动。

### 本阶段范围

本阶段只允许新增以下文档及进行文档一致性校验：

- `docs/contract_risk_review_design.md`
- `docs/contract_risk_review_stage_log.md`

本阶段不允许修改业务代码、配置、依赖、数据库、Docker Compose、OpenAPI 或 Java–Python DTO。

### 已冻结内容

1. `RiskReviewPlanBuilder`属于 Contract Python 确定性业务编排，不是 Framework Stage。
2. Framework 只新增一个隐藏模型执行单元：`execute_risk_review_bundle`。
3. 五个旧 Risk Stage ID 保留为确定性 Adapter，不再调用模型、Tool、ReAct 或重新读取 IR。
4. 五个基础 Review Unit 和两个候选驱动的横向 Review Unit 固定。
5. `PlaybookManifest`、`CheckSpec`、`RiskReviewPlan`、`ReviewUnitSpec`、`RiskReviewContext`、`FindingDraft`、`ReviewUnitResult`、`RiskReviewBundle`的职责和校验规则固定。
6. 一期所有正式启用检查默认 `REQUIRED`；`OPTIONAL`和`ADVISORY`默认不进入正式结果。
7. 每个分配的 `check_code`必须且只能返回一个 `REVIEWED`、`NOT_APPLICABLE`或`FAILED`状态。
8. 逐 Finding 的 `domain + check_code + category -> legacy_artifact_type`映射固定；禁止按粗粒度 Unit 映射。
9. 未映射、冲突映射和非显式 `OTHER`必须失败，不允许兜底、静默丢弃或隐藏降级。
10. 横向检查先由代码确定性产生候选；没有候选时不调用模型。
11. Direct Structured Review 固定 `temperature=0`、关闭 Thinking、Tool=0、严格 Schema；Schema 修复仅限当前 Unit 一次。
12. 基础 Unit 受控并发；Specialist 最多 2 次且必须由现有 Finding 或确定性候选触发。
13. 输入、输出、调用次数和整体时延预算固定，并纳入超限失败门禁。
14. 新增 `LlmRuntime.complete_with_usage()`观测协议，同时保留原 `complete() -> str`兼容。
15. Anchor、Evidence、Finding Merge、结果 Hash、Java DTO、OpenAPI 和 `schema_version=1.0`继续复用，不在本阶段改变。
16. Shadow Compare 固定比较关键风险召回、Evidence、Schema、重复率、视角、Token、耗时和调用次数。
17. `legacy/window/playbook`配置开关、回滚方式和失败恢复路径固定。
18. 阶段 6.1～6.9 的实施顺序、每阶段测试门禁和停止点固定。
19. 阶段 6.0 结束后必须停止，不自动进入阶段 6.1。
20. `FVA-005 -> OTHER -> rights_obligations_review_result`作为唯一待用户确认的显式 `OTHER`映射，不得充当通用兜底。

### 校验结果

- 两份Markdown围栏数量均为偶数，结构完整。
- 设计稿包含阶段6.0要求的20类冻结内容和阶段6.1～6.9门禁。
- 映射表共45条：组合键45个、`check_code` 45个，均无重复。
- 五类`legacy_artifact_type`全部属于当前代码已经存在的Artifact；非法Artifact为0。
- `OTHER`仅出现1次，即文档明确列出的`FVA-005`待确认映射。
- 当前差异严格限定为本阶段两份文档，没有修改业务代码、配置、依赖或Compose。
- `git diff --check`通过。

### 提交与远程

本阶段使用中文提交信息，推送目标为`origin/feat/contract-risk-review-playbook-v1`。实际Commit SHA和远程状态以Git记录及阶段交付报告为准。

### 进入阶段 6.1 的门禁

- 阶段 6.0 文档校验全部通过。
- 提交并推送远程功能分支。
- 用户确认阶段 6.0 设计以及 `FVA-005`的显式 `OTHER`映射。
- 用户明确下达开始阶段 6.1 的指令。

## 阶段 6.1：Playbook 与 PlanBuilder

状态：未开始。

目标：实现固定模型、基础Manifest、Router、确定性PlanBuilder、Plan Hash、隐藏Plan接口、适用性和完整性门禁；不得新增模型调用。

开始前：重新完整阅读联合冻结稿、`contract_risk_review_design.md`和本记录。

## 阶段 6.2：LlmRuntime 可观测结果

状态：未开始。

目标：先以`commercial_financial`完成Direct A/B，同时增加兼容的`complete_with_usage()`，验证Tool=0、正常1次模型调用、Token、首Token、模型耗时、Trace和Provider请求标识。

## 阶段 6.3：基础 Review Bundle

状态：未开始。

目标：扩展到五个基础Unit，完成Direct Structured Review、受控并发、严格Check覆盖和Bundle校验。

## 阶段 6.4：横向候选与 Specialist

状态：未开始。

目标：实现两个横向维度的确定性候选生成、无候选0调用、按需裁决和最多两次Specialist约束。

## 阶段 6.5：领域扩展示例

状态：未开始。

目标：实现`software_ip` Playbook样例，验证适用性、注入和调用预算，不修改DAG、Adapter或正式DTO。

## 阶段 6.6：Bundle Adapter 与现有合并

状态：未开始。

目标：将Bundle逐Finding确定性映射回五类旧Artifact并接入现有阶段5.1合并；验证全集守恒和安全边界。

## 阶段 6.7：Shadow Compare

状态：未开始。

目标：使用固定合同集合比较Legacy与Direct的质量、证据、重复率、Token、耗时和调用次数。

## 阶段 6.8：测试环境切换和全链路验收

状态：未开始。

目标：仅在独立测试环境切换Direct，完成Java→Python→Framework真实链路、失败注入、预算、取消、Attempt、Sink、Hash、回滚和目标时延验证。

## 阶段 6.9：稳定化与清理建议

状态：未开始。

目标：在用户另行确认正式切换且稳定后提出Legacy清理建议；任何正式切换和删除都需单独确认。
