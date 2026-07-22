# 合同 Risk Review Playbook 化阶段记录

本文记录合同审查 Risk Review 层阶段 6 的实施状态、基线、验证结果和进入下一阶段的门禁。唯一架构设计依据为同目录下的 `contract_risk_review_design.md`；Java–Python–Framework 对外协议仍以唯一联合冻结稿为准。

## 工作约束

- 阶段 6 最初从阶段 5.4 提交`b471c3624956899706c9aca7d379340cad17e6af`建立；确认Window工作结束后，已迁移到阶段5.5最终提交`7e4e9760d8bdc04374c26d8692151a0399574e50`。
- 分支：`feat/contract-risk-review-playbook-v1`。
- 工作树：`D:\contract-risk-review-v1`。
- 不修改、测试、提交或覆盖原 Window 工作树中正在进行的改动。
- 每个实施阶段开始前重新阅读联合冻结稿、阶段 6 设计稿和本记录。
- 每个阶段必须先完成本阶段测试、记录结果并推送远程功能分支，再进入下一阶段。
- 未经用户明确确认，不合并 `main`、不部署正式环境。

## 阶段 6.0：设计冻结

状态：通过。

### 基线

- Window最终分支：`feat/contract-ir-window-v1`。
- 旧Window基线：`b471c3624956899706c9aca7d379340cad17e6af`，阶段5.4。
- 新Window最终基线：`7e4e9760d8bdc04374c26d8692151a0399574e50`，`修复：合同窗口支持增量重试与显式值补全`，阶段5.5。
- Window本地工作树干净，本地HEAD与GitHub远程分支HEAD均为`7e4e9760`；全过程只读，未在Window工作树修改、提交、重置或覆盖文件。
- 原阶段6.0提交：`ea68a7f08f29d143d5e7d7f76eb0852d501e6014`。
- 本地备份分支：`backup/contract-risk-review-stage6.0-ea68a7f`。
- rebase后阶段6.0提交：`fabd5fd006bc0a0dcd3236aabce213d8e144883f`。
- rebase结果：无冲突；提交图为`7e4e976 → fabd5fd`，两份阶段6.0设计文件完整保留。

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
20. `FVA-005 -> OTHER -> rights_obligations_review_result`已经确认，是唯一允许的显式`OTHER`映射，不得充当通用兜底；内部`risk_type`固定为`MANDATORY_RULE_OR_VALIDITY_RISK`。

### 固定Fixture核验

- 只读目录：`/home/aituge/workspace/contract-review-dev/test-artifacts/risk-review-input`。
- 已完整阅读`README.md`；没有下载、修改、覆盖或加入Git。
- `SHA256SUMS`验证：三个JSON和README全部`OK`。
- 标准Stage Artifact：`result_type=CONTRACT_IR_STAGE_V1`；14个IR分类数组合计101项。
- 完整Fixture：12个Window，12个Mapped Extraction；Coverage为93/93 Block、12/12 Section、12/12 Window，无失败Window。
- 固定上下文：杭州戎一教育科技有限公司、苏州爱兔格人工智能科技有限公司、`PARTY_A`、`NEUTRAL`。
- 阶段6.1测试从该Artifact直接进入PlanBuilder；不得重新运行parse、resolve_parties或extract_contract_ir，外部模型调用必须为0。

### 校验结果

- 两份Markdown围栏数量均为偶数，结构完整。
- 设计稿包含阶段6.0要求的20类冻结内容和阶段6.1～6.9门禁。
- 映射表共45条：组合键45个、`check_code` 45个，均无重复。
- 五类`legacy_artifact_type`全部属于当前代码已经存在的Artifact；非法Artifact为0。
- `OTHER`仅出现1次，即已经确认的`FVA-005`受限兼容映射。
- 当前差异严格限定为本阶段两份文档，没有修改业务代码、配置、依赖或Compose。
- `git diff --check`通过。

### 提交与远程

本阶段使用中文提交信息，推送目标为`origin/feat/contract-risk-review-playbook-v1`。实际Commit SHA和远程状态以Git记录及阶段交付报告为准。

### 进入阶段 6.1 的门禁

- 阶段 6.0 文档校验全部通过。
- 提交并推送远程功能分支。
- 基线迁移和固定Fixture/预算设计修订已单独提交并推送。
- 用户明确下达开始阶段 6.1 的指令。

## 阶段 6.1：Playbook 与 PlanBuilder

状态：通过；已停止在阶段 6.1，不自动进入阶段 6.2。

目标：实现固定模型、基础Manifest、Router、确定性PlanBuilder、Plan Hash、隐藏Plan接口、适用性和完整性门禁；不得新增模型调用。

固定输入：服务器标准`CONTRACT_IR_STAGE_V1` Artifact、固定Risk Context和完整Window/Block/Anchor Fixture。直接生成Plan，不执行parse、主体解析、IR抽取、旧风险Agent、Tool或LLM。

开始前：重新完整阅读联合冻结稿、`contract_risk_review_design.md`和本记录。

### 已实现

- 新增严格`PlaybookManifest`、`CheckSpec`、`RiskReviewPlan`、`ReviewUnitSpec`、`RiskReviewContext`及相关枚举和校验模型。
- 固定45项一期检查及逐Finding兼容映射；检查编号和映射均唯一。
- `FVA-005`是唯一允许映射为`OTHER`的检查，内部`risk_type`固定为`MANDATORY_RULE_OR_VALIDITY_RISK`；注册时不满足该条件直接失败。
- 新增`PlaybookRegistry`和`PlaybookRouter`：基础包必选、重复/未知/停用/不适用包失败、Specialist最多2个、`EXTEND_DOMAIN`只能复用既有七个审查单元。
- 新增确定性`RiskReviewPlanBuilder`：按检查所需IR类型投影、验证真实Block和Anchor、生成逐字Source Excerpt、Context Hash、Plan Hash和稳定ID。
- 五个基础Unit固定存在；两个横向Unit由确定性候选驱动，没有候选时直接产生11项`REVIEWED/NO_DETERMINISTIC_CANDIDATES`，不生成模型Batch。
- 模型预算投影使用Context内稳定短Evidence引用，Python保留并映射真实Anchor、Block、字符区间、原文和Hash；没有截断、删除或改变任何IR与Source Excerpt。
- 超过硬预算时按`check_code`执行确定性最小Batch分组；排序规则依次为Batch数量、最大Batch Token和检查编号，输入相同则结果完全一致。
- 新增隐藏Plan接口`POST /v1/internal/contract-reviews/{review_id}/risk-plan`；`include_in_schema=false`，固定公开OpenAPI未增加路径或Schema。
- Fixture Loader仅位于测试目录，必须显式传入目录并核对三个固定SHA-256；生产代码没有默认Fixture路径或回退逻辑。
- 没有引入依赖、数据库变更、Compose变更、模型客户端、HTTP调用、LLM调用或Tool调用；未修改Window、阶段5.1、Java、公开DTO和`schema_version=1.0`。

### 固定Fixture结果

- 输入：101项IR、93个Block、12个Window，固定`PARTY_A/NEUTRAL`上下文。
- Plan ID：`risk-plan-a7b1227bd27984e4cf63ca2e433bf452`。
- Plan Hash：`sha256:a7b1227bd27984e4cf63ca2e433bf452fef822601fb2f88dd6eb3891565b3eac`。
- Review Unit：7个，其中5个基础Unit、2个横向Unit。
- 检查：45项完整分配且无重复；无候选横向确定性结果11项。
- 正常模型Batch计划：7个；横向无候选模型调用：0；Specialist：0。
- Batch Token估算：基础Unit全部不超过6,000硬限制；实际范围3,984～5,826。
- 每个模型Context投影均少于完整101项IR；Anchor逐字回查全部通过。
- 相同输入连续100次得到完全相同Plan；PlanBuilder P95=`334.801ms`，低于1秒门禁。

### 服务器隔离验证

- 测试只在服务器隔离源码目录和已有Python测试镜像执行，没有覆盖`python-source`、`python-ir-window-source`或运行中的正式/联调容器。
- 固定Fixture以只读Volume挂载；没有下载到本地，没有改动、覆盖或重新生成。
- 未重新运行parse、resolve_parties、Window IR抽取或旧风险Agent；外部模型调用为0。
- 针对性Plan/API/OpenAPI测试：34项通过（中间门禁）。
- 最终Contract Python全量测试：`117 passed, 10 skipped`。
- 固定运行时OpenAPI与冻结文件一致；隐藏Plan接口明确不出现在公开OpenAPI。
- `git diff --check`通过；阶段6.1代码中不存在模型Runtime、OpenAI、HTTP客户端或`.complete()`调用。

### 阶段结论

阶段6.1门禁全部通过。当前只提交并推送功能分支，不合并`main/proof`，不部署测试或正式运行容器；后续阶段6.2必须在重新阅读联合冻结稿、设计稿和本记录并获得下一步指令后开始。

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
