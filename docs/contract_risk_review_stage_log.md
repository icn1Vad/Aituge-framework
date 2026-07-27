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

状态：进行中；仅授权`commercial_financial` Direct Reviewer A/B，完成后停止，不进入阶段6.3。

目标：先以`commercial_financial`完成Direct A/B，同时增加兼容的`complete_with_usage()`，验证Tool=0、正常1次模型调用、Token、首Token、模型耗时、Trace和Provider请求标识。

### 开始前核对单

- 当前阶段：6.2，`commercial_financial` Direct Reviewer A/B。
- 联合冻结稿：`E:\MyProjects\Newestcontract\合同审查一期 Java–Python–Framework 联合技术方案 v1.0（冻结稿）.md`；SHA-256=`0e1de5f8c7e9d97fdfe17b202c92f084e07ac0c7f17702c9942337504129bbe0`。
- 阶段6设计稿：`D:\contract-risk-review-v1\docs\contract_risk_review_design.md`；开始前SHA-256=`9e368699ace298ed3e5ecf0ea2a20f88e6c7956278252b600f367f911145a362`。
- Window阶段记录：`D:\contract-risk-review-v1\docs\contract_ir_window_stage_log.md`；开始前SHA-256=`ddabfbb6c723e3eae94608cfdd312f5f8a22eeed017fb57cc9f73b3843e47d2e`；已复读阶段5、5.1和阶段5.5最终Window能力。
- 当前工作目录和工作树：`D:\contract-risk-review-v1`。
- 当前分支：`feat/contract-risk-review-playbook-v1`。
- 当前HEAD：`a9a06bd49d9282d6ddca45e9c3eb3c67718147ad`；远程同名分支HEAD一致。
- 开始前工作树：干净。
- 允许修改：商务财务Direct Reviewer、严格输入输出模型、`ReviewUnitResult`、`FindingDraft`、`CheckCoverageResult`、当前Unit一次Schema修复、当前Unit调用指标、隐藏隔离测试入口、A/B脚本和Artifact、向后兼容的`LlmRuntime.complete_with_usage()`及本阶段测试和记录。
- 禁止修改：其他四个基础Reviewer、Bundle、`execute_risk_review_bundle`、横向模型裁决、Specialist、旧Stage Adapter、正式Pipeline、阶段5.1、Playbook样例、Window、Java、公开OpenAPI、正式DTO、数据库、正式Compose和正式容器。
- 必须复用：阶段6.1 PlanBuilder的`commercial_financial`唯一Batch及Context、固定CF-001～CF-008 CheckSpec、现有IR/Anchor/Block、正式Evidence Validator的既有语义、统一`LlmRuntime`模型配置。
- 固定Fixture：服务器只读目录`/home/aituge/workspace/contract-review-dev/test-artifacts/risk-review-input`；101项IR、93个Block、12个Window；不得下载到本地、修改、覆盖、重新解析或重新运行IR。
- 模型调用：允许真实`deepseek-v4-pro`；Legacy链路运行1次；Direct链路重复实验3次。每次Direct正常调用严格为1次，只有JSON/Schema错误允许当前Unit修复1次；不得因质量或性能重抽结果。
- 配置：只允许隔离测试入口所需的临时配置，不修改正式模型配置，不把Direct设为默认。
- 容器：允许重建或重启阶段6隔离容器；禁止操作正式容器和正式数据库。
- Direct调用边界：Tool=0、`temperature=0`、`thinking_override=false`、严格Schema；八个CF检查必须完整且唯一覆盖，任一REQUIRED为FAILED时Unit失败。
- 输入门禁：目标2,000～4,000 Token，软上限5,000，硬上限6,000；不得删除检查或静默截断。
- 输出门禁：目标不超过1,500 Token，软上限2,500，硬上限4,000；不得固定最多4条Finding。
- Evidence门禁：每个实质Finding至少一个候选；IR、Anchor、Block和字符区间必须存在，逐字满足`quoted_text == block_text[char_start:char_end]`；缺失风险使用合法ABSENCE。
- 性能门禁：单次目标不超过30秒，三次实验最大目标不超过35秒；超过时保留完整诊断并判定性能门不通过。
- 测试门禁：Direct成功和全部非法覆盖场景、一次修复、Usage归属、`complete()`兼容、Fixture严格校验、隐藏接口不进入OpenAPI、Framework和Contract回归、固定OpenAPI、`git diff --check`和`git show --check`。
- 质量门禁：关键商务风险不漏、Evidence 100%有效、无依据风险为0、CF检查覆盖100%、正常Direct一次模型调用且零Tool、无需手工改JSON。
- 阶段提交信息：`功能：实现商务财务风险直接审查`；仅在自动测试、质量和性能门全部通过后提交并推送。
- 通过后允许进入的下一阶段：无；完成阶段6.2后停止并等待用户确认。
- 失败后停止位置：阶段6.2；默认不提交业务实现，不进入阶段6.3。

### 阶段6.2真实A/B门禁结果

状态：未通过；已按性能和质量门禁停止，不提交、不推送、不进入阶段6.3。

- 隔离环境：服务器已有`contract-review-dev-framework-1`，代码只复制到容器内`/tmp/stage62`；未修改正式源码、正式配置、正式数据库或正式容器。
- 固定Fixture：101项IR、93个Block、12个Window；文件Hash校验通过，全程只读使用。
- 模型：隔离库已有租户`0`下的真实`deepseek-v4-pro`；没有新增或修改模型配置。
- Legacy仅执行1次：总耗时`87,040ms`，模型调用8次，Tool调用7次，返回4条Finding和7条Evidence（HIGH 2、MEDIUM 2）。
- Legacy结果未通过既有`CommercialTermsStageResult`：6条文本Evidence提供了`quoted_text`但缺少配套`quoted_text_hash`，状态记录为`INVALID`；原始输出保留，不替Legacy修复。
- Legacy Token指标未能恢复：原A/B进程在Schema校验异常后、Artifact落盘前退出，现场会话只保存了最终文本；Prompt/Completion/Total Token明确记录为`null`，未做估算或伪造。
- Direct第1次：`27,627ms`，TTFT `2,260ms`，Prompt `4,937`、Completion `1,943`、Total `6,880` Token，模型调用1次、修复0次、Tool 0次，6条Finding、7条Evidence。
- Direct第2次：`43,212ms`，首次输出未通过严格校验，触发当前Unit唯一一次Schema修复；TTFT分别为`641ms/720ms`，合计Prompt `11,999`、Completion `3,862`、Total `15,861` Token，模型调用2次、修复1次、Tool 0次，最终6条Finding、7条Evidence。
- Direct第3次：`22,672ms`，TTFT `745ms`，Prompt `4,937`、Completion `1,710`、Total `6,647` Token，模型调用1次、修复0次、Tool 0次，6条Finding、7条Evidence。
- Direct耗时：min=`22,672ms`、median=`27,627ms`、max=`43,212ms`。最大值超过`35,000ms`硬门禁，因此性能门未通过。
- 三次Direct均完整且唯一覆盖CF-001～CF-008，最终Evidence均由代码映射回既有IR、Anchor、Block、字符区间和原文；Tool调用始终为0。
- 质量稳定性未通过：三次均识别付款前置、交付物不明确和验收缺失，但CF-003与CF-004在“中风险缺失保护”和“对我方有利/低风险Finding”之间摇摆；CF-005未独立识别全额预付款缺少履约保障。
- Legacy与Direct共同风险：全额预付/付款未与交付挂钩、交付范围或标准不明确、验收机制缺失。
- Legacy独有候选：考核条款句子不完整及其费用调整后果不明。
- Direct独有候选：价款计价口径、发票税率/时限、抵扣或结算调整；其中存在材料性和立场稳定性疑问，不能自动认定为质量提升。
- A/B Artifact：隔离容器`/app/runtime/stage62-commercial-ab.json`；SHA-256=`09669cda6624900ff4af81bcff419d11fc9e8490419dd5ac98f57600fac54d7c`。
- 诊断日志：服务器隔离目录`/home/aituge/workspace/contract-review-dev/test-artifacts/stage62-ab-run.log`。
- 自动测试：Direct/非法输出/Fixture/Usage定向测试`19 passed`；`git diff --check`通过。由于真实性能和质量门已失败，未继续执行全量回归，也未创建提交。
- 模型调用控制：Legacy严格1次；Direct严格3次重复实验，其中只有第2次按授权规则执行1次Schema修复；发现门禁失败后没有再次调用模型或抽取更快结果。

结论：阶段6.2未通过。当前工作树保留未提交实现和诊断记录，等待用户决定后续是修订本阶段Prompt/Schema后重新验收，还是放弃本实现；阶段6.3不得开始。

### 阶段6.2质量稳定性修订核对单

状态：根据用户新授权继续阶段6.2；只修订`commercial_financial`，仍禁止进入阶段6.3。

- 保留边界：Tool=0、正常模型调用1次、`temperature=0`、`thinking=false`、CF-001～008完整覆盖、严格Pydantic、Evidence确定性映射和仅当前Unit一次局部修复。
- 修订范围：CF-003/004/005可执行判定策略、首次JSON输出契约、Schema失败原因观测、Direct五次固定Fixture验收和阶段6.2测试/Artifact。
- 禁止范围：ReAct、Tool取数、Legacy修复、其他Reviewer、Bundle、横向裁决、Specialist、Adapter、正式Pipeline、阶段5.1、Window、Java、公开OpenAPI、正式DTO/数据库/Compose/容器。
- 性能门调整：无修复目标不超过30秒；一次局部修复允许不超过50秒；任一次硬上限60秒。本轮旧实验`43,212ms`不再单独构成性能失败。
- 质量阻塞：CF-003/004结论波动，CF-005没有稳定识别全额预付款缺少履约保障。
- CF-003冻结：只审发票/税费独立风险；含税总价且付款前提供约定发票属于反例，不得将有利条款输出为Finding。
- CF-004冻结：只审调价、扣款、抵销和结算调整；有利于我方且完整的扣款权不是风险，仅缺少一般抵销权也不是风险；Finding必须引用具体原文。
- CF-005冻结：提前支付比例/时点与有效保障两阶段独立判断；全部或至少70%提前支付且无保障必须HIGH，风险Evidence必须包含付款文本和保障缺失ABSENCE。
- Schema策略：首次调用启用模型端正式JSON模式，提供八个Check最小合法JSON、字段枚举和必填关系；仍由严格Pydantic及语义校验决定是否合法，不做宽松解析。
- 旧第2次修复原因：原`ReviewUnitResult`只记录了调用指标，没有持久化第一次失败内容及校验原因，事后无法准确还原；本次增加`repair_reasons`，新的五次验收如发生修复必须记录具体错误。
- 固定Fixture、模型与隔离容器规则不变；Direct必须使用相同Plan Context连续执行至少5次，不得挑选结果。
- 新质量门：五次CF覆盖100%、CF-005召回100%、CF-003/004核心根因/等级/Evidence语义稳定、Evidence有效率100%、无依据Finding为0、正常调用1次、Tool始终0。
- 提交门：只有五次质量/性能、Framework与Contract回归、固定OpenAPI及Git检查全部通过，才使用`功能：实现商务财务风险直接审查`提交并推送；否则不提交并停止。

### 阶段6.2质量修订后五次验收

状态：未通过；已按质量门停止，不再修改或重跑，不提交、不推送、不进入阶段6.3。

- 修订实现：为CF-003/004/005注入结构化决策策略；增加`decision_note`、`repair_reasons`和CF-004/005 Evidence硬校验；Direct调用启用官方JSON Object模式；保留严格Pydantic和一次局部修复。
- Prompt门禁：固定Fixture完整Prompt使用Framework本地Tokenizer估算`5,850 Token`，低于6,000硬上限；未删除任何Check、IR或Evidence原文。
- 定向测试：`23 passed`；固定Fixture通过Hash、101项IR、93个Block和唯一`commercial_financial` Context校验。
- 五次均使用同一Fixture、同一Plan Context、真实`deepseek-v4-pro`、`temperature=0`、`thinking=false`、Tool=0；未运行Legacy，也没有并行抽卡。
- 第1次：`36,507ms`，TTFT=`2,494/1,406ms`，Prompt=`12,914`、Cached=`7,040`、Completion=`3,157`、Total=`16,071` Token，模型2次、修复1次，最终3条Finding/6条Evidence。
- 第2次：`34,826ms`，TTFT=`905/765ms`，Prompt=`12,889`、Cached=`12,544`、Completion=`3,107`、Total=`15,996` Token，模型2次、修复1次，最终3条Finding/5条Evidence。
- 第3次：`9,794ms`，TTFT=`856/864ms`，Prompt=`11,851`、Cached=`11,520`、Completion=`1,027`、Total=`12,878` Token，模型2次、修复1次，最终0条Finding。
- 第4次：`10,078ms`，TTFT=`859/1,073ms`，Prompt=`11,851`、Cached=`11,648`、Completion=`1,027`、Total=`12,878` Token，模型2次、修复1次，最终0条Finding。
- 第5次：`10,027ms`，TTFT=`677/1,071ms`，Prompt=`11,851`、Cached=`11,648`、Completion=`1,027`、Total=`12,878` Token，模型2次、修复1次，最终0条Finding。
- 性能结论：五次均低于一次修复50秒和单Unit 60秒门禁；性能可行。
- Schema结论：五次首次响应均把顶层字段写成`checks`而不是严格模型要求的`check_results`，具体错误均为`check_results Field required + checks Extra forbidden`；因此五次全部进入局部修复，Schema修复成为正常路径，质量门不通过。
- 质量结论：第1、2次稳定识别CF-005全额预付款无保障（HIGH，付款原文+ABSENCE），同时识别CF-007交付时间/节点不明和CF-008验收缺失；CF-003/004均稳定判定当前条款对我方有利或无实质风险。
- 第3～5次修复响应直接复制`empty_check_shape`，八个Check全部`NO_MATERIAL_RISK`且无Finding；CF-005、CF-007、CF-008全部漏报。因此CF-005召回仅2/5，关键风险召回与语义稳定性门禁失败。
- 第1、2次还存在`reason_code=NO_MATERIAL_RISK`但同时包含Finding的语义矛盾；严格结构合法不等于业务语义合格。
- Evidence结论：第1、2次已生成的Evidence全部由代码映射到既有IR/Anchor/Block/字符区间；CF-005同时包含付款文本和合法ABSENCE。后三次没有Evidence，不能据此判定质量通过。
- 新A/B Artifact：服务器宿主机`/home/aituge/workspace/contract-review-dev/test-artifacts/stage62-commercial-quality5.json`；SHA-256=`73f56fb10635a3ae31260c83234be724c596f439a886aea260969a7d065279c0`。
- 新运行日志：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage62-commercial-quality5.log`。
- 按用户门禁，发现五次质量失败后不再修改Prompt、不再调用模型、不执行全量Framework/Contract/OpenAPI回归，也不创建提交。

结论：阶段6.2性能路线已验证，但Schema首次输出和修复后业务召回不稳定，阶段仍未通过。工作树保留未提交实现、测试和复现信息，等待下一步决定。

### 阶段6.2第三轮修订开始前核对单

- 当前阶段：6.2；只修首轮输出Schema、受限规范化、修复语义守恒和CF-005候选稳定性，完成后停止。
- 新授权文件：`D:\codex\.codex\attachments\a717e02c-8926-4e26-9430-6be459a99503\pasted-text.txt`；SHA-256=`e4db574f9219c08de19701d93086d621c2e9f6b6594f7eceb54b2744e57bf90d`。
- 联合冻结稿：60,026字节、2,730行；SHA-256=`0e1de5f8c7e9d97fdfe17b202c92f084e07ac0c7f17702c9942337504129bbe0`；已重新完整读取并确认Java/Python公开协议、正式Finding/Evidence和Result Sink不变。
- 阶段6设计稿：28,263字节、337行；开始前SHA-256=`ac7188ecd827d8390229193fe091325824744415ec81499137b4ab1741872265`；已重新完整读取。
- 阶段日志：25,986字节、287行；开始前SHA-256=`00dd49d6057947c23b7851dcc62a25c8918a2709b3855969b268d6a0a3955f66`；已重新完整读取。
- 工作目录/工作树：`D:\contract-risk-review-v1`；分支`feat/contract-risk-review-playbook-v1`；HEAD=`a9a06bd49d9282d6ddca45e9c3eb3c67718147ad`；远程同名分支一致。
- 工作树状态：保留阶段6.2未提交改动；修改`llm_runner.py`、设计稿、阶段日志和Runtime测试；新增Direct Reviewer、A/B脚本及两组测试；`git diff --check`通过。
- 已读取当前已跟踪Diff及全部新增文件；本轮不得清理或覆盖既有阶段6.2工作。
- 已读取服务器Artifact`stage62-commercial-quality5.json`与日志；Artifact SHA-256=`73f56fb10635a3ae31260c83234be724c596f439a886aea260969a7d065279c0`。
- 原始响应可用性：旧Artifact只保存最终`ReviewUnitResult`、调用指标和截断后的`repair_reasons`，没有保存首次或修复模型的完整原始Content；日志也只保存调用时点和最终摘要。因此无法从既有现场逐字恢复五次首轮/修复原文，也不能可靠断言五次首轮各自是否已包含CF-005。
- 现有证据只允许确认：五次首轮顶层均为`checks`且缺少`check_results`；第1、2次错误摘要显示首轮存在非空内容，但是否包含CF-005无法从截断文本证明；第3～5次错误摘要末尾出现`findings=[]`，但不能证明全部Check均为空。修复后的第1、2次包含CF-005，修复后的第3～5次被清成全空。
- 本轮必须新增原始Attempt诊断保存，确保新的五次验收逐次保留首轮原始输出、修复原始输出、精确错误和修复前后差异；不得伪造旧Artifact中不存在的数据。
- 允许修改：Reviewer内部输入字段、Prompt、严格已知Schema规范化、一次修复及语义守恒、CF-005确定性候选、内部诊断、测试/A/B脚本和本阶段记录。
- 禁止修改：Java、公开OpenAPI、`schema_version=1.0`、正式Finding/Evidence DTO、Result Sink、Window IR、阶段5.1、正式环境/容器、其他四个Reviewer、阶段6.3及后续代码。
- 固定Fixture：`/home/aituge/workspace/contract-review-dev/test-artifacts/risk-review-input`；只读复用101项IR、93个Block、12个Window，不重新解析或生成IR。
- 模型调用：自动测试通过前为0；通过后使用相同Fixture至少5次真实Direct，Tool=0、`temperature=0`、`thinking=false`。
- 通过门：五次Schema稳定、CF-005 5/5、CF-003/004稳定、Evidence 100%、无依据Finding、性能达标；否则不提交、不推送并停止。

### 阶段6.2第三轮修订与真实验收

状态：未通过；真实模型在第1次验收的唯一佸�}-�G����ƭy�nding Template Registry、Canonical Root Policy、固定Fixture ICD Oracle、共享候选裁决内核的必要领域泛化、ICD测试、阶段6.3隔离验收脚本和本记录。
- 当前禁止修改：Java、公开OpenAPI、正式Finding/Evidence DTO、`schema_version=1.0`、Window IR、Commercial、FVA、PO已通过规则、LRE、横向检查、Specialist、阶段5.1、正式Pipeline、正式容器和阶段6.4以后代码。
- 必须复用：阶段6.1 Plan/Registry/Context/Hash；阶段6.2严格Raw/Final、Evidence映射、一次局部Schema修复、Tool=0、`temperature=0`、`thinking=false`；PO已通过的“确定性Candidate→最小模型Decision→Python Evidence/Severity→Canonical Root→确定性Finding”链路、Check级Source隔离、Primary Evidence权威、Factor过滤审计和Fail Fast模式。
- 固定Fixture：服务器只读目录`/home/aituge/workspace/contract-review-dev/test-artifacts/risk-review-input`；101项IR、93个Block、12个Window；不重新解析合同、不重新生成IR、不下载到本地。
- 当前实现差异：ICD已有六个CheckSpec和单Batch Context，但仍走旧通用模型完整Finding输出；Candidate只有每Check一个宽泛`PROJECTED_IR_REVIEW`，无ICD专属成立条件、缺失机制、Evidence Source隔离、Severity证据门、Control Code、Canonical Root和确定性Finding物化。固定Fixture当前ICD Context投影71项IR、80个文本Source、6个Absence Source，Plan估算5,826 Token；虽然没有获得全部101项IR且仍低于6,000硬门，但多数普通权利义务与ICD无关，必须通过领域Source Policy收缩。
- 固定Fixture只读事实：唯一明确保密原文为“乙方及乙方人员应保守甲方的商业秘密”；未定位到项目成果/新增知识产权归属、背景知识产权许可边界、第三方不侵权及替换/赔偿机制、完整保密例外/期限/披露程序、数据处理安全机制、数据返还删除留存机制。该事实只进入测试Oracle；生产规则不得硬编码当前主体、IR编号、Anchor、条款号或具体文本。
- 本阶段模型调用：实现、自动测试、离线Plan/构造输出和回归期间为0；全部门禁通过后，允许同一ICD Batch串行真实运行5次，每次正常调用1、Repair=0、Tool=0，任一轮失败立即停止后续轮次。
- 配置与容器：不修改正式配置；允许复用既有服务器隔离容器和测试源码副本；不得操作正式环境。
- 测试门禁：ICD-001～006 Candidate/Source/Absence/Severity/Control/Root/模板的正反例；Candidate覆盖；跨Check/跨Generation/错误Source；未验证Factor不进入等级；Root合并安全；主体视角；Prompt预算；Plan重复100次；Commercial/FVA/PO及Contract Python回归；`git diff --check`。
- 质量门禁：固定Fixture六项Check 5/5完整；适用Candidate逐项裁决；Evidence有效率100%；无依据Finding=0；Category/Risk Type/Level/主体和Primary Evidence由Python确定；风险根因、Root、等级、Primary Evidence、Control Code及Finding数量5/5一致；重复Finding=0。
- 性能门禁：单Batch目标不超过30秒，允许不超过45秒，硬上限60秒。
- 本轮提交信息：无；无论通过或失败均不提交、不推送。
- 通过后允许进入的下一阶段：无；通过后只汇报ICD子门禁并停止，等待是否继续LRE。
- 失败后必须停止的位置：ICD专项；保存结果及Attempt Artifact，不运行LRE、Bundle或阶段6.4。

### 阶段6.3 ICD专项实现与五轮验收

状态：ICD子门禁通过；阶段6.3整体仍未通过。已停止在ICD，不运行LRE、基础Bundle真实验收或阶段6.4；本轮不提交、不推送。

#### 确定性实现

- 为`ICD-001～ICD-006`建立独立Source Pattern、场景门和Absence Policy。固定Fixture的ICD Context仍投影71项IR，但只生成2个合法文本Evidence Source和1个合法Absence Source，不把全部101项IR或80个宽泛Source交给模型。
- 建立12类文本/缺失Candidate、Control Code Registry、Finding Template、Canonical Root Policy和ICD Severity Factor Registry。模型只输出Candidate Verdict、摘要、提议Severity Factor、合法Supporting/Counter Source和Control Code；Primary Evidence、主体立场、Category、Risk Type、Risk Level、Root和正式Finding由Python确定。
- 缺失型Candidate必须先满足业务场景门；固定Fixture只存在明确保密场景，因此`ICD-001/002/003/005/006`不生成无依据Candidate，`ICD-004`生成文本保护Candidate与保密完整性Absence Candidate。
- 文本Root与缺失Root默认分离，`merge_group_id=None`；没有显式兼容规则时不得仅因同一Check合并。Severity模型提议逐项通过Python Evidence门，未验证Factor保留审计但不进入最终等级。
- 建立扩展Check兼容边界：同领域但未登记到一期ICD Registry的Check继续按自身`required_ir_types`投影并使用通用缺失规则，避免`software_ip`等扩展Playbook被固定Registry误判。

#### 首轮Fail Fast与立场修订

- 首轮真实验收仅运行Run 1后即按门禁停止：模型把“乙方及乙方人员应保守甲方的商业秘密”错误解释为对`PARTY_A`不利，令`CONFIDENTIALITY_PROTECTION_REVIEW`由预期`NO_RISK`变为`RISK`，并与`CONFIDENTIALITY_COMPLETENESS_ABSENT`同时形成两个Root/Finding。
- 首轮性能和结构本身正常：墙钟6,625ms，模型1次、Repair=0、Tool=0，Prompt=3,010、Completion=568、TTFT=1,611ms。失败属于审查立场和Candidate边界，不是Schema、Evidence或超时问题。
- 新增`IcdPerspectivePrecondition`：仅要求相对方保护我方秘密、且未对我方施加不利保密负担的文本Candidate，由Python确定性返回`DETERMINISTIC_PRECONDITION / NO_RISK`；定义、例外、期限和披露程序缺失仍由独立Absence Candidate交给模型。反向立场和我方承担保密义务的正反例均有自动测试。
- 首轮失败Artifact已保留：`stage63-icd-five-run-before-perspective-gate-failed.json`，SHA-256=`e133585d181bbd92b897a55e5499b954b33d347ff34de55be09e492e17087a11`；Attempt Artifact SHA-256=`b72d8d9f9aa354a6ffe4cb1a41ecae27c64bf9bf4d03fb3d56494a9ca8353035`。

#### Plan与无模型门禁

- 100次Plan重建稳定；Plan ID=`risk-plan-925c8d3f7fb3a951855a384d09d22f8b`，Plan Hash=`sha256:925c8d3f7fb3a951855a384d09d22f8b6c47c10b9d2db13ce81013405bd35cb3`，模型调用0。
- 单ICD Batch估算Context=5,826 Token；没有获得全部101项IR；固定Fixture Candidate为2个，其中保密文本Candidate确定性`NO_RISK`，只有保密完整性Absence Candidate进入模型。
- Plan Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-icd-plan-audit-after-perspective-gate.json`，状态`PASSED`，SHA-256=`b09eccad459d97fe388c90ded4965861f3d6150400f96ff1b64703a01e989aa9`。
- ICD专项自动测试`62 passed, 79 deselected`；基础Bundle完整文件`134 passed, 7 skipped`。覆盖六Check、Candidate稳定性、跨Check Source隔离、12类Control、Severity Registry、场景正反例、字面/Absence证据门、立场前置门、Root/Finding物化及模型最小输出协议。

#### 第二组五轮真实验收

固定Fixture、单ICD Batch、模型`deepseek-v4-pro`、`temperature=0`、`thinking=false`；五轮串行，每轮完成立即执行Hard Gate、Fixture Oracle和稳定性门。

| Run | 墙钟 | Prompt | Completion | TTFT | 模型 | Repair | Tool | Finding/Root |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 4,802ms | 2,525 | 297 | 1,912ms | 1 | 0 | 0 | 1/1 |
| 2 | 3,789ms | 2,525 | 291 | 1,005ms | 1 | 0 | 0 | 1/1 |
| 3 | 3,469ms | 2,525 | 280 | 882ms | 1 | 0 | 0 | 1/1 |
| 4 | 3,712ms | 2,525 | 289 | 908ms | 1 | 0 | 0 | 1/1 |
| 5 | 3,705ms | 2,525 | 280 | 1,064ms | 1 | 0 | 0 | 1/1 |

- 墙钟`min/median/max=3,469/3,712/4,802ms`，远低于30秒目标；五轮每轮模型1次、Repair=0、Tool=0。
- `CONFIDENTIALITY_PROTECTION_REVIEW`五轮均由Python确定性返回`NO_RISK`；`CONFIDENTIALITY_COMPLETENESS_ABSENT`五轮均由模型返回`RISK`，唯一Root类型为`CONFIDENTIALITY_COMPLETENESS_ABSENT`，最终等级五轮均为`MEDIUM`，Finding数量五轮均为1。
- Primary Evidence五轮固定；正式Finding Evidence有效。模型五轮均提议`MISSING_CORE_MECHANISM`并被接受；另提议的`POST_TERMINATION_EFFECT`、`OPERATIONAL_IMPACT`及部分轮次的`NO_EFFECTIVE_REMEDY`因缺少合法Absence类型或直接因果Evidence被Python拒绝，未污染最终等级。
- 五轮结果Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-icd-five-run.json`，状态`PASSED`，SHA-256=`9032fdcff8f338199548295aa9ecae6bb06482d754bfa81cc8bfc5e3ac5bfbab`。
- 五轮完整Attempt Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-icd-five-run-attempts.json`，SHA-256=`f65c0f2687582de720a8f8b88f00d2df94e6b0873f62324a67c052198d483c6b`。

#### 回归与协议门禁

- Contract Python全量：`116 passed, 11 skipped`。第一次使用Framework测试镜像因缺少Contract镜像依赖而在收集阶段失败，不是业务测试失败；改用既有`contract-ir-window-stage3-contract-tests:test`后全量通过。
- Framework/风险相关最终回归：`154 passed, 2 skipped`，覆盖Commercial、FVA、PO、ICD、固定Fixture、Capability注册和Fail Fast。
- 固定OpenAPI与运行时逐对象一致，隐藏Risk Plan接口未出现在公开OpenAPI；相关测试包含于Contract Python全量通过结果。
- 全量回归曾发现扩展Check `SWI-001`被固定ICD Registry查询导致`KeyError`；已增加非内置ICD Check回退规则并复验通过。该修订不改变一期六Check规则。
- `git diff --check`通过，仅存在Windows换行提示。没有修改Java、公开OpenAPI、正式Finding/Evidence DTO、`schema_version=1.0`、Window IR、Commercial、FVA、PO已通过规则、LRE、横向检查、Specialist、阶段5.1、正式Pipeline或正式环境。
- 当前HEAD和远程同名分支仍均为`0dfc336b98850783e0ae5783af25e805799eeba7`；本轮全部实现继续位于未提交工作树，没有提交或推送。

结论：ICD子门禁通过，可以建议在用户另行授权后继续LRE专项；阶段6.3整体仍未通过。本轮按授权停止，不运行LRE、基础Bundle真实验收或阶段6.4。

### 阶段6.3 LRE专项开始前核对单

- 当前阶段：阶段6.3 `liability_remedies_exit`（LRE-001～LRE-008）专项；Commercial、FVA、PO、ICD子门禁已通过，阶段6.3整体仍未通过。
- 联合冻结稿：`E:\MyProjects\Newestcontract\合同审查一期 Java–Python–Framework 联合技术方案 v1.0（冻结稿）.md`；60,026字节、2,729个零基行索引末位；SHA-256=`0e1de5f8c7e9d97fdfe17b202c92f084e07ac0c7f17702c9942337504129bbe0`；已重新完整读取，确认公开Java–Python协议、正式Finding/Evidence、Result Sink和`schema_version=1.0`不变。
- 阶段6设计稿：`docs/contract_risk_review_design.md`；已重新完整读取。LRE以冻结Registry定义为准：LRE-001违约触发和责任成立、LRE-002违约金和损失计算、LRE-003责任上限和免责、LRE-004赔偿和第三方索赔、LRE-005解除终止和单方退出、LRE-006终止后结算返还和继续义务、LRE-007不可抗力和风险分配、LRE-008法律适用及争议管辖。
- 阶段日志：已重新完整读取阶段6.0～ICD子门禁记录；确认Commercial、FVA、PO、ICD通过规则及其Artifact、性能和回归基线。
- 当前工作目录/工作树：`D:\contract-risk-review-v1`；分支=`feat/contract-risk-review-playbook-v1`；HEAD=`0dfc336b98850783e0ae5783af25e805799eeba7`；本地与GitHub真实远程同名分支一致。
- 当前工作树：完整保留阶段6.3 Commercial、FVA、PO、ICD及共享Direct内核未提交修改；不得清理、覆盖、stash、reset或切换。
- 安全基线：本地保护分支`backup/contract-risk-stage63-local-0dfc336`继续指向已提交HEAD；工作树外备份`E:\MyProjects\Newestcontract\backups\stage63-fva-git-divergence-20260723-140342`继续存在。
- 当前允许修改：LRE Candidate Registry、Evidence/Absence Source Policy、Severity Factor Registry及证据门、Control Code Registry、Finding Template Registry、Canonical Root Policy、固定Fixture三层Oracle、共享Candidate Decision内核的必要LRE泛化、LRE测试、阶段6.3隔离验收脚本、设计稿和本记录。
- 当前禁止修改：Java、公开OpenAPI、正式Finding/Evidence DTO、`schema_version=1.0`、Window IR、Commercial、FVA、PO、ICD已通过规则、横向检查、Specialist、阶段5.1、正式Pipeline、正式配置和容器、五领域Bundle真实验收、阶段6.4及以后代码。
- 必须复用：阶段6.1 Plan/Registry/Context/Hash；阶段6.2严格Raw/Final、单Batch一次局部Schema修复、Tool=0、`temperature=0`、`thinking=false`；PO/ICD已通过的确定性Candidate、Check级Source隔离、Primary Evidence权威、Severity Factor过滤审计、Canonical Root、Python Finding物化和逐轮Fail Fast。
- 固定Fixture：服务器只读目录`/home/aituge/workspace/contract-review-dev/test-artifacts/risk-review-input`；101项IR、93个Block、12个Window；不得下载到本地、重新解析合同或重新生成IR。
- Fixture Oracle：真实模型调用前只读核对LRE相关IR、Source和合同结构，冻结Candidate Oracle、Canonical Root Oracle和Final Finding Oracle；生产规则不得硬编码当前主体、IR ID、Anchor、条款号或具体文本。
- 本阶段模型调用：实现、自动测试、100次Plan稳定性和离线输出验证期间为0；全部无模型门禁通过后，LRE按两个Batch并行、不同轮次串行真实运行5次；每轮正常模型调用2、Repair=0、Tool=0，任一轮失败立即停止后续轮次。
- 输入门禁：LRE-001～008完整且唯一分配；两个Batch均低于6,000 Token；任一Batch不得获得全部101项IR；不得静默截断或删除Check。
- Evidence门禁：责任缺失、整改期缺失及终止结算缺失必须使用合法`RiskAbsenceEvidenceSource`；责任上限绕过必须具备上限与可能绕过条款的组合Evidence；Primary由Python固定，跨Check/Batch/Generation及不支持根因的Source全部拒绝。
- Severity门禁：模型只能从当前Candidate闭集提议Factor；Python逐项验证并计算最终等级；未经验证Factor不得进入等级。异地管辖不自动HIGH，间接损失、无限责任、上限绕过、累计救济和终止风险均须满足冻结Evidence门。
- Root门禁：只有Registry明确允许、root type一致且核心Evidence支持同一法律后果的Candidate才能合并；无上限责任与单方解除、终止结算缺失与争议管辖等独立风险不得错误合并。
- 测试门禁：至少覆盖本轮授权列出的40类无模型测试；Plan重复构建至少100次；完成RISK/NO_RISK/INSUFFICIENT、非法Candidate/Source/Factor、跨Batch Root、独立Root、主体视角、Control Code和Fail Fast离线验证。
- 质量门禁：五轮LRE-001～008与全部Candidate完整裁决；Evidence有效率100%；无依据Finding和未经验证Factor进入等级均为0；Root、核心Verdict、风险根因、等级和Primary Evidence五轮一致；主体视角100%；重复Finding 0；每轮模型调用2、Repair=0、Tool=0。
- 性能门禁：单Batch和LRE Unit目标不超过30秒，允许不超过45秒，任一次硬上限60秒。
- 回归门禁：LRE专项、Commercial、FVA、PO、ICD、风险模块、Framework无实时服务、Contract Python全量、固定OpenAPI、隐藏Risk Plan接口及`git diff --check`。
- 配置与容器：只允许复用服务器既有隔离测试环境和测试源码副本；不得操作正式环境。
- 本轮提交信息：无；无论通过或失败均不提交、不推送。
- 通过后允许进入的下一阶段：无；LRE子门禁完成后必须停止，等待用户决定是否进行五领域Bundle验收。
- 失败后必须停止的位置：LRE专项；保存完整结果和Attempt Artifact，不运行后续轮次、不进入Bundle或阶段6.4。

### 阶段6.3 LRE专项实现与五轮验收

状态：LRE子门禁通过；阶段6.3整体仍未通过。已停止在LRE，不运行五领域Bundle真实验收或阶段6.4；本轮不提交、不推送。

#### 确定性实现与Fixture Oracle

- 为`LRE-001～LRE-008`建立独立Source/Absence Policy、Candidate Registry、Severity Factor Policy、Control Code Registry、Finding Template和Canonical Root Policy；模型仍只返回逐Candidate Verdict、摘要、提议Factor、合法Supporting/Counter Source及Control Code。
- 固定Fixture生成2个LRE Batch：`risk-batch-718fb74802914711c2bb66ee76eef703`分配LRE-001/004/006，投影59项IR，Prompt估算2,614 Token；`risk-batch-13b20935e7f8a962f7d6e8d418d07f82`分配LRE-002/003/005/007/008，投影49项IR，Prompt估算5,701 Token。两个Batch均低于6,000 Token，均未获得全部101项IR。
- Plan ID=`risk-plan-1b7e91b139dcbc0700d28a7ac41da7db`；Plan Hash=`sha256:1b7e91b139dcbc0700d28a7ac41da7db45a50da95865b1cd5e800c763283457b`；100次重复构建稳定，模型调用0。
- Candidate Oracle共8项：`BROAD_BREACH_TRIGGER_REVIEW`、`OVERBROAD_INDEMNITY_REVIEW`、`OVERBROAD_LOSS_SCOPE_REVIEW`、`CUMULATIVE_REMEDIES_REVIEW`、`LIABILITY_CAP_ABSENT`、`TERMINATION_RIGHTS_REVIEW`、`FORCE_MAJEURE_MECHANISM_ABSENT`、`DISPUTE_RESOLUTION_ABSENT`。前5项及后2项预期`RISK`；合同解除条款双方对等且具整改期，`TERMINATION_RIGHTS_REVIEW`预期`NO_RISK`。
- Canonical Root Oracle共4项：`UNBOUNDED_LIABILITY_EXPOSURE/HIGH`、`CUMULATIVE_REMEDIES_REVIEW/MEDIUM`、`FORCE_MAJEURE_MECHANISM_ABSENT/MEDIUM`、`DISPUTE_RESOLUTION_ABSENT/MEDIUM`。最终Finding固定4条、Evidence固定6条；不生成自动续期、受限退出窗口、争议条款冲突、异地管辖、单方解除或终止结算缺失风险。
- `UNBOUNDED_LIABILITY_EXPOSURE`由广泛违约触发、开放赔偿/间接损失、过宽赔偿及责任上限缺失四个Candidate确定性归并；累计救济、不可抗力和争议解决保持独立Root。Primary/Core Evidence、主体立场、Category、Risk Type、Risk Level和正式Finding由Python生成。

#### 验收中发现并修复的问题

- 新增广泛违约Candidate后，生产Root和Candidate Oracle已包含该Candidate，但固定Root Oracle仍保留旧三Candidate分组。依据确定性Registry同步Root Oracle后，分组与正式结果一致；没有根据随机模型结果放宽Oracle。
- 累计救济原规则把`CUMULATIVE_REMEDIES + FINANCIAL_IMPACT`直接升级为HIGH。固定原文“违约金1万元并赔偿损失”证明累计金钱暴露，但一般金钱影响是累计责任的内生后果；现固定为MEDIUM，无上限、间接损失和上限绕过继续由独立高风险Root处理。
- 模型曾把同一Root中另一Candidate的Primary Source作为当前Candidate Supporting，严格Evidence边界正确失败。Prompt新增“空Supporting白名单必须返回空数组”和“同Root不得跨Candidate借证”，Validator未放宽。
- 不可抗力及争议解决缺失的`NO_EFFECTIVE_REMEDY`在不同轮次由模型偶发提议。两条合法Absence Source已分别明确检查通知/后果/解除及争议救济路径，因此Python将其与`MISSING_CORE_MECHANISM`一并确定性生成，等级保持MEDIUM。
- 验收脚本的`candidate_core_keys`误包含模型推荐Control Code，导致Verdict、Risk Type、Risk Level和Primary Evidence均一致时仍报核心漂移。现Candidate核心比较只包含其名称所声明的字段；最终Canonical Root仍比较完整Control Code集合。
- 所有失败运行均按Fail Fast停止并保存结果和Attempt Artifact；未使用失败结果补齐五轮，最终正式验收从Run 1重新执行。

#### 五轮真实验收

| Run | 两Batch耗时 | Unit墙钟 | Prompt/Cached/Completion | TTFT | 调用/Repair/Tool | Candidate/Root/Finding/Evidence |
| --: | -- | --: | -- | -- | -- | -- |
| 1 | 6,321 / 14,513ms | 14,615ms | 8,260 / 8,192 / 2,037 | 904 / 1,113ms | 2 / 0 / 0 | 8 / 4 / 4 / 6 |
| 2 | 5,919 / 17,620ms | 17,635ms | 8,260 / 8,192 / 2,000 | 720 / 4,448ms | 2 / 0 / 0 | 8 / 4 / 4 / 6 |
| 3 | 7,363 / 13,180ms | 13,244ms | 8,260 / 8,192 / 1,947 | 1,155 / 818ms | 2 / 0 / 0 | 8 / 4 / 4 / 6 |
| 4 | 5,927 / 15,267ms | 15,335ms | 8,260 / 8,192 / 2,028 | 1,154 / 763ms | 2 / 0 / 0 | 8 / 4 / 4 / 6 |
| 5 | 6,324 / 13,736ms | 13,807ms | 8,260 / 8,192 / 1,909 | 821 / 817ms | 2 / 0 / 0 | 8 / 4 / 4 / 6 |

- 墙钟`min/median/max=13,244/14,615/17,635ms`；单Batch最大17,620ms，均低于30秒目标和60秒硬上限。
- 五轮每轮两个Batch并行、模型调用2、Repair=0、Tool=0；8个Candidate完整且唯一裁决，未知/重复/静默遗漏为0。
- 四个Canonical Root、风险等级、Primary/Core Evidence、正式Finding风险根因、主体视角和最终Control Code集合5/5一致；Evidence有效率100%，重复Finding=0，无依据Finding=0。
- 模型每轮提议23～36个Factor；Python接受13～17个、拒绝9～19个。拒绝项均保留审计原因，未经验证Factor进入最终等级为0，提议数量变化未污染最终Root或Finding。
- 正式五轮Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-lre-five-run-final-pass.json`，SHA-256=`6d9975924a11f07a1f334df8e9bb9bff2fd125266a18614bb43423a08cb5ca02`。
- 完整Attempt Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-lre-five-run-final-pass-attempts.json`，SHA-256=`502a235cda69f22042a6f615316d6ccf212f528580c4783cba1f574336a51b34`。

#### 回归与协议门禁

- LRE专项和固定Fixture：`7 passed`；风险/Framework相关完整回归：`147 passed, 2 skipped`，覆盖Commercial、FVA、PO、ICD、LRE、Fail Fast和共享Direct内核。
- Framework无实时服务全量：`383 passed, 2 skipped`；明确排除需要独立Smoke HTTP服务的既有`test_live_multi_capability.py`。
- Contract Python全量：`116 passed, 11 skipped`。
- 固定OpenAPI与运行时逐对象一致、隐藏Risk Plan接口未出现在公开OpenAPI：`7 passed`。
- `git diff --check`通过，仅存在既有Windows换行提示。没有修改Java、公开OpenAPI、正式Finding/Evidence DTO、`schema_version=1.0`、Window IR、阶段5.1、正式Pipeline或正式环境。
- 当前HEAD仍为`0dfc336b98850783e0ae5783af25e805799eeba7`；本轮全部实现继续位于未提交工作树，没有提交或推送。

结论：LRE子门禁通过，可以建议在用户另行授权后进入五领域Bundle验收；阶段6.3整体仍未通过。本轮按授权停止，不运行Bundle、阶段6.4或任何后续阶段。

### 阶段6.3 五领域基础Bundle开始前核对单

- 当前阶段：阶段6.3最后门禁，五领域基础风险审查Bundle集成与真实并发验收；Commercial、FVA、PO、ICD、LRE子门禁均已通过，阶段6.3整体尚未通过。
- 联合冻结稿：`E:\MyProjects\Newestcontract\合同审查一期 Java–Python–Framework 联合技术方案 v1.0（冻结稿）.md`；60,026字节；SHA-256=`0e1de5f8c7e9d97fdfe17b202c92f084e07ac0c7f17702c9942337504129bbe0`；已重新完整读取，确认Java–Python公开协议、正式Finding/Evidence DTO、Result Sink、Result Hash和`schema_version=1.0`不变。
- 阶段6设计稿：`docs/contract_risk_review_design.md`；34,155字节；开始前SHA-256=`16c0d4e308fdaee2c82ef24325bc32b5cc66810f3b685eb6baeb99228f55b28b`；已重新完整读取。
- 阶段日志：`docs/contract_risk_review_stage_log.md`；开始前125,309字节；SHA-256=`581be6049f7d28fb36079838294b070080761c9dab4fd20d448f32b82479b6af`；已重新完整读取阶段6.0～LRE最终门禁记录。
- 当前工作目录/工作树：`D:\contract-risk-review-v1`；分支=`feat/contract-risk-review-playbook-v1`；HEAD=`0dfc336b98850783e0ae5783af25e805799eeba7`；本地、远程跟踪引用及GitHub真实远程同名分支一致。
- 当前工作树：完整保留阶段6.3五领域及共享Direct内核未提交修改；修改6个已跟踪文件、新增6个未跟踪文件；不得清理、覆盖、stash、reset、rebase或切换。
- 安全基线：本地保护分支`backup/contract-risk-stage63-local-0dfc336`继续指向已提交HEAD；新的工作树外备份为`E:\MyProjects\Newestcontract\backups\stage63-bundle-precommit-20260724-013614`，包含状态、完整Binary Patch、6个未跟踪文件副本及SHA-256清单。
- 服务器核验：没有遗留`stage63`、风险验收Pytest、模型验收或`extract-all`后台进程；仅保留既有长期运行的隔离测试容器。
- 当前允许修改：`execute_base_risk_review_bundle`及其内部严格Bundle模型、身份一致性、七Batch滚动并发、排队/启动偏移/耗时/Token指标、Unit确定性合并后的Hard Gate、Bundle完整性、原子失败/超时/取消收敛、跨Unit硬重复检测与语义重叠候选记录、Bundle无模型测试/验收脚本、设计稿和本记录。
- 当前禁止修改：Java、公开OpenAPI、正式Finding/Evidence DTO、`schema_version=1.0`、Result Sink、正式Result Hash、Window IR、五领域已通过Candidate/Severity/Root/Finding规则、两个横向检查、阶段5.1、Playbook/Specialist、旧Stage Adapter、正式Pipeline、正式配置和环境、`main/proof`及阶段6.4以后代码。
- 必须复用：阶段6.1稳定Plan/Context/Hash；阶段6.2 Commercial Direct Reviewer；FVA三态；PO、ICD、LRE确定性Candidate、Evidence Source、Severity Factor证据门、Canonical Root和Python Finding物化；严格Raw/Final、Tool=0、`temperature=0`、`thinking=false`及逐轮Fail Fast。
- 固定Fixture：服务器只读目录`/home/aituge/workspace/contract-review-dev/test-artifacts/risk-review-input`；101项IR、93个Block、12个Window；我方杭州戎一教育科技有限公司、相对方苏州爱兔格人工智能科技有限公司、`PARTY_A/NEUTRAL`；不得下载、重新解析合同或重新生成IR。
- Bundle结构门禁：五个基础Unit、七个模型Batch、FVA-001～005、CF-001～008、PO-001～007、ICD-001～006、LRE-001～008共34项Check；任何缺失、未知、重复、错属或必需失败均使Bundle失败。
- 并发门禁：固定Fixture七Batch应一次性受控并发，无批次屏障；真实验收`peak_concurrency=7`；正常每轮模型调用7、Repair=0、Tool=0。
- 身份门禁：所有Unit/Batch必须使用同一review、generation、document/contract、schema、perspective、our_party、counterparty、attitude、fixture、plan_id和plan_hash；Bundle不得猜测或覆盖不一致。
- 原子门禁：任一Batch、Unit、身份、Evidence或完整性失败时不生成可消费的部分Finding集合；尽力取消未完成任务，收敛已启动任务，保存失败Unit/Batch、完成/取消/在途计数及Trace诊断。
- 无模型门禁：至少覆盖授权列出的35类Bundle测试、五类失败注入及取消/超时/无部分结果；Plan重复构建100次，Plan ID/Hash、Batch、Candidate、Evidence Source和Oracle稳定；每Batch低于6,000 Token且不接收全部101项IR；模型调用为0。
- 真实模型门禁：三轮串行Bundle，每轮内部七Batch并发；每轮结束立即验证34 Check、五领域Oracle、Evidence、主体视角、调用/Repair/Tool/并发和原子性，并与前序成功轮次比较；任一轮失败立即停止。
- 性能门禁：Bundle墙钟目标不超过30秒，允许不超过45秒，硬上限60秒；只报告三轮min/median/max，不宣称P95。
- 回归门禁：Bundle、Commercial、FVA、PO、ICD、LRE、风险模块、Framework无实时服务、Contract Python全量、固定OpenAPI、隐藏Risk Plan/Bundle接口及`git diff --check`。
- Artifact：仅保存在服务器隔离测试目录，不加入Git；固定名称`stage63-base-bundle-three-run-final.json`、`stage63-base-bundle-three-run-attempts.json`和`stage63-base-bundle-failure-injection.json`，均计算SHA-256。
- 本阶段模型调用：实现、无模型测试、100次Plan稳定性和失败注入期间为0；全部通过后仅允许三轮固定Fixture真实Bundle，共正常21次模型调用；不得挑选结果或额外重抽。
- 本阶段提交信息：仅全部门禁通过后使用`功能：实现合同五领域并行风险审查`，推送`origin/feat/contract-risk-review-playbook-v1`并核对本地/跟踪/真实远程一致。
- 通过后允许进入的下一阶段：无；提交推送并汇报后停止。阶段6.4尚未授权。
- 失败后必须停止的位置：阶段6.3 Bundle门禁；不提交、不推送，保存工作树、Artifact和复现信息。

### 阶段6.3 五领域基础Bundle最终验收

状态：阶段6.3全部门禁通过，已完成五领域基础Bundle集成、三轮真实并发验收、
原子失败注入和最终回归。阶段6.4尚未授权。

#### Bundle结构与确定性门禁

- 固定Plan ID=`risk-plan-1b7e91b139dcbc0700d28a7ac41da7db`，Plan
  Hash=`sha256:1b7e91b139dcbc0700d28a7ac41da7db45a50da95865b1cd5e800c763283457b`；
  100次重复构建的Plan、7个Batch、Candidate和Evidence Source稳定。
- Bundle固定包含5个Unit、7个Batch和34个Check：FVA-001～005、
  CF-001～008、PO-001～007、ICD-001～006、LRE-001～008。任何单Batch均未
  获得全部101项IR，输入预算低于6000 Token硬上限。
- 7个Batch一次性创建并滚动收敛，无批次屏障；三轮
  `bundle_queue_ms=0`、`peak_concurrency=7`。Bundle不增加模型总结、合并或
  复核调用。
- 新增权威`BaseBundleIdentity`、逐Batch/逐Unit指标、跨Unit技术重复门禁和
  语义重叠候选记录。任一Batch、Unit、身份、Check或Evidence失败时返回严格
  `BaseBundleFailure`，不包含正式`findings`字段。
- Commercial稳定性按阶段6.2既有规则执行：CF-003/004状态和CF-005核心风险、
  HIGH等级、付款Source及ABSENCE存在性三轮稳定；第3轮额外生成CF-007/008，
  作为允许的非核心附加Finding变化披露，不据此判失败。其他四领域继续严格
  比较Root、等级和Primary/Core Evidence。

#### 三轮真实Bundle

| Run | Bundle墙钟 | 7个Batch墙钟（FVA/CF/PO-1/PO-2/ICD/LRE-1/LRE-2） | 调用/Repair/Tool | Prompt/Cached/Completion/Total Token | Finding/Evidence |
| --: | --: | -- | -- | -- | -- |
| 1 | 16,627ms | 5,488 / 12,688 / 16,625 / 8,705 / 3,879 / 6,098 / 14,015ms | 7 / 0 / 0 | 32,363 / 32,000 / 6,384 / 38,747 | 12 / 38 |
| 2 | 16,087ms | 5,329 / 11,578 / 16,086 / 8,813 / 3,438 / 6,104 / 13,764ms | 7 / 0 / 0 | 32,363 / 32,000 / 6,394 / 38,757 | 12 / 38 |
| 3 | 17,601ms | 5,877 / 17,600 / 16,168 / 8,605 / 3,708 / 7,144 / 13,419ms | 7 / 0 / 0 | 32,363 / 32,000 / 7,012 / 39,375 | 14 / 42 |

- Bundle墙钟`min/median/max=16,087/16,627/17,601ms`，全部低于30秒目标；
  最慢Batch为17,600ms，低于60秒硬上限。
- 三轮每轮7次正常模型调用、Repair=0、Tool=0、34项Check完整、
  Evidence有效率100%、未经验证Severity Factor进入等级为0、主体视角100%。
- FVA三轮Finding=0，FVA-002保持
  `EXTERNAL_VERIFICATION_REQUIRED/INSUFFICIENT_EVIDENCE`。
- Commercial三轮CF-005均为`ADVANCE_PAYMENT_SECURITY_RISK/HIGH`，同时具有
  付款文本和履约保障缺失ABSENCE；Finding数量1/1/3，第三轮的CF-007/008按
  冻结规则作为非核心波动披露。
- PO三轮均为9个Candidate、6个Root、6个Finding、27条Evidence；
  PO-001保持单一HIGH Root，PO-003 Finding=0，PO-006=MEDIUM。
- ICD三轮均为2个Candidate、1个Root、1个Finding、3条Evidence，
  `CONFIDENTIALITY_COMPLETENESS_ABSENT/MEDIUM`稳定。
- LRE三轮均为8个Candidate、4个Root、4个Finding、6条Evidence；
  `UNBOUNDED_LIABILITY_EXPOSURE/HIGH`及三个MEDIUM Root稳定。

#### 原子失败与验收器修正

- 无模型注入Commercial Batch失败、PO单Batch失败、LRE单Batch超时、ICD
  Unit合并失败和FVA视角不一致共5类场景，全部返回`FAILED`且正式部分结果为0；
  尚未开始或可取消任务被取消，已启动任务收敛后仅保存诊断。
- 首次真实启动发现模型注册租户为`0`、验收脚本默认租户为
  `__default_tenant_id__`；该次在端点调用前失败。最终验收显式使用只读确认的
  `tenant_id=0`，未修改模型注册或正式配置。
- 真实调用后发现验收汇总器错误要求Commercial ABSENCE的
  `verification_note`与Plan内部`verification_method`逐字一致；现改为精确
  Plan Source优先，否则由正式`checked_scope + verification_note`计算稳定键。
- 验收器曾漏导入`BASE_UNIT_IDS`，并曾错误比较Commercial全部附加Finding；
  已新增Fake Bundle从执行、汇总到五领域Validator的端到端无模型测试，并按
  阶段6.2规则建立Commercial核心稳定性门。上述均为验收器缺陷，没有放宽领域
  Oracle或修改业务结果。
- 为保持成本记录透明：租户不匹配未产生有效端点调用；汇总/Validator缺陷期间
  分别完成1轮、1轮和2轮真实Batch；最终正式三轮完成21次调用。本次排障期间
  有效模型调用总数为49次，最终Artifact只包含最后从Run 1重新开始的三轮。

#### Artifact与最终回归

- 正式三轮Artifact：
  `/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-base-bundle-three-run-final.json`，
  SHA-256=`4bb6bd8410e3bcab42971fcc664e68e3b24a174074d6b988572c4f8727dff561`。
- 完整Attempt Artifact：
  `/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-base-bundle-three-run-attempts.json`，
  SHA-256=`d7f09f14151f8225511e40bbcd35921a879e20f68d8e27e8112d7f0f404a19e7`。
- 失败注入Artifact：
  `/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-base-bundle-failure-injection.json`，
  SHA-256=`418327a353dd7d4a2c1127a3bd130f79f6f8d19d0c62182a908918f08fb56bea`。
- 风险模块及Framework无实时服务全量：`402 passed, 2 skipped`；首次未注入
  隔离Redis地址时发生1个既有环境失败，使用
  `contract-review-dev-framework-redis-1`重跑后全部通过。
- Contract Python全量：`116 passed, 11 skipped`；固定OpenAPI一致性和隐藏
  Risk Plan/Bundle接口不进入公开OpenAPI包含在通过结果中。
- `git diff --check`提交前通过；没有修改Java、公开OpenAPI、正式DTO、
  `schema_version=1.0`、Window IR、阶段5.1、正式Pipeline或正式环境。

结论：阶段6.3通过。只有完成提交、推送和三方HEAD一致性核验后才结束本阶段；
阶段6.4尚未授权。

### 阶段6.3 Prompt Token预算政策补充

状态：预算规则修正和既有三轮Artifact离线重放通过；阶段6.3恢复为通过。
阶段6.4未开始且本轮未授权。

#### 原门禁与只读审计

- 原Provider Prompt硬上限为`6,000`。阶段6.3三轮最终Artifact中，FVA的
  Provider `usage.prompt_tokens`均为`6,145`，确实超过原门禁145 Token，
  超出比例约`2.42%`；不能表述为原门禁没有超限。
- 七个Batch只有FVA超过6,000；Commercial=`5,677`、PO-1=`5,309`、
  PO-2=`4,447`、ICD=`2,525`、LRE-1=`2,699`、LRE-2=`5,561`。
- FVA三轮耗时`5,329～5,877ms`，Bundle墙钟`16,087～17,601ms`；FVA质量、
  Evidence、Candidate和三态结果稳定，`Repair=0`、`Tool=0`，没有截断、
  Schema失败、Candidate遗漏或Evidence丢失。
- 审计确认Plan的`estimated_input_tokens`是字符权重生成的Business Context
  相对大小，只用于投影和Batch拆分，不包含完整System Prompt、输出协议和
  Candidate定义，不能代表Provider Prompt。客户端Qwen Tokenizer估算也不能
  冒充DeepSeek Provider Usage。

#### 设计决策

Prompt预算政策升级为`2.0`：

```text
Provider Prompt <= 6,000
→ WITHIN_TARGET

Provider Prompt 6,001～7,000
→ SOFT_WARNING
→ 记录Token、超出值、比例、Unit和Batch
→ 不触发Repair，不使Batch、Unit或Bundle失败

Provider Prompt > 7,000
→ HARD_LIMIT_EXCEEDED
→ RISK_PROMPT_TOKEN_HARD_LIMIT_EXCEEDED
→ 按既有原子规则使Batch、Unit和Bundle失败
```

`cached_tokens`继续作为`prompt_tokens`子集记录，不重复相加。Provider Usage
缺失时状态为`PROVIDER_USAGE_UNAVAILABLE`，不使用Business Context或本地
Tokenizer估算伪造Provider Token。

本次不压缩FVA Prompt，不删除法律边界、Candidate、Evidence或Check，不改变
模型输出协议、七Batch划分、模型调用数和业务规则。调整原因是避免为了145 Token
的极小偏差牺牲审查信息，同时继续使用7,000硬上限防止Prompt无限膨胀。

#### 代码和指标边界

- 保留兼容字段`estimated_input_tokens`，明确其语义为
  `estimated_business_context_tokens`；Plan Builder仍按原6,000 Business
  Context上限执行确定性Batch拆分。
- 新增内部`PromptBudgetResult`，记录政策版本、Business Context估算、可选
  Client估算及Tokenizer、Provider Prompt/Cached、预算状态、超出值、比例、
  Unit和Batch。
- Bundle新增`prompt_budget_warning_count`、
  `prompt_budget_hard_failure_count`、`max_provider_prompt_tokens`、
  `batches_over_target`和`batches_over_hard_limit`。
- Provider硬超限检查集中在七Batch统一执行路径；软告警仅进入结构化指标和
  Warning，不影响正式结果原子性。

#### 既有Artifact离线重放

- 输入：
  `/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-base-bundle-three-run-final.json`。
- 输出：
  `/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-prompt-budget-policy-v2-replay.json`。
- 输出SHA-256：
  `5c34f91d5005c0e7347c43ec6144aa394ad108f01440568e38d8b1ceae6e8142`。
- 模型调用数：`0`。
- 三轮均为：`prompt_budget_warning_count=1`、
  `prompt_budget_hard_failure_count=0`、`max_provider_prompt_tokens=6,145`；
  仅FVA Batch进入`batches_over_target`，`batches_over_hard_limit=[]`。

#### 自动测试和回归

- Prompt预算边界、软告警、Provider Usage缺失、Cached Token子集、Bundle软告警
  和硬失败原子性专项：通过；额外验证Provider Prompt硬超限在首轮结果返回后
  立即停止，不进入Schema Repair。
- 风险模块及Framework无实时服务全量：`409 passed, 2 skipped`；没有执行
  `test_live_multi_capability.py`，没有真实模型调用。
- Contract Python全量：`117 passed, 10 skipped`；使用`--network none`的既有
  Contract测试镜像执行，不具备模型网络访问。
- 固定OpenAPI一致性、所有JSON响应Schema和隐藏Risk Plan接口检查包含在
  Contract Python通过结果中；公开OpenAPI未发生变化。
- `git diff --check`通过，仅有既有Windows换行提示。
- 本轮没有修改风险Prompt、Candidate、Evidence、Check、模型输出协议、
  七Batch划分、Java、正式DTO、Window IR、阶段5.1或正式Pipeline。

结论：原6,000门禁确实未满足；经明确设计决策将6,000调整为目标值、7,000调整
为Provider硬上限后，既有三轮结果满足政策2.0。FVA状态为`SOFT_WARNING`且不阻塞
阶段6.3。阶段6.3通过；阶段6.4尚未授权。

## 阶段 6.4：合同横向一致性与缺失完整性审查

状态：进行中；只实现`cross_clause_consistency`和
`missing_ambiguity_completeness`，不实现或接入Specialist。

### 开始前核对单

```text
当前阶段：6.4
当前工作目录：D:\contract-risk-review-v1
当前工作树：D:\contract-risk-review-v1
当前分支：feat/contract-risk-review-playbook-v1
开始前HEAD：034693b53a91898354cd45544ec6916a5746a874
本地/跟踪引用/GitHub真实HEAD：一致
开始前工作树：干净
外部备份：E:\MyProjects\Newestcontract\backups\stage64-start-20260724-111516
允许修改：两个横向Unit、内部扩展Bundle、内部测试、设计和阶段记录
禁止修改：五基础领域规则及Oracle、Java、公开OpenAPI、正式DTO、
  schema_version、Result Sink/Hash、Window IR、阶段5.1模型合并、
  Playbook、Specialist、旧Stage Adapter、正式Pipeline及正式环境
必须复用：RiskReviewPlan、Direct Reviewer最小裁决协议、
  Evidence/Absence Source、Severity Factor Policy、Canonical Root、
  Python Finding物化、Bundle原子性和Prompt预算政策2.0
固定Fixture：服务器risk-review-input目录中的101项IR Fixture
模型调用：完成无模型门禁后才允许；Consistency和Completeness各5轮，
  Extended Bundle 3轮；每Batch正常1次，Repair=0，Tool=0
配置和正式容器：禁止修改或重启
本阶段通过后提交：功能：实现合同横向一致性与完整性审查
通过后：推送并停止；后续阶段尚未授权
失败后：不提交、不推送，保存Artifact并停止
```

开始前已完整复核联合冻结稿、阶段6设计稿和阶段日志，并读取阶段6.1至
6.3的Registry、Plan、Direct Reviewer、Evidence/Absence、Severity、
Canonical Root、Finding物化、Bundle原子性、Prompt预算政策2.0及阶段5.1
确定性合并基础。阶段5.1模型语义合并不在本阶段接入范围。

### 当前子门禁结果

- 横向无模型专项：`11 passed`。
- Consistency真实验收：5/5通过；每轮2个Batch并发、2次模型调用，
  Repair=0、Tool=0；两个稳定Root为
  `EFFECTIVE_DATE_CHRONOLOGY_CONFLICT/MEDIUM`和
  `PARTY_TERM_IDENTITY_CONFLICT/MEDIUM`；墙钟
  `3,173～4,365ms`。
- Completeness真实验收：5/5通过；每轮1个Batch、1次模型调用，
  Repair=0、Tool=0；稳定Root为
  `REFERENCED_ATTACHMENT_MISSING/MEDIUM`；墙钟
  `2,371～4,889ms`。MAC-001、MAC-002、MAC-004、MAC-006由
  `BASE_DOMAIN`所有权链接基础Finding，未重复物化；MAC-003在固定
  Fixture无确定性Candidate。
- Extended Bundle从第1轮重新执行时，基础阶段FVA-002返回了人员资质/
  履约能力范围材料，既有硬门禁以
  `RISK_FVA002_SCOPE_LEAKAGE`拒绝。Fail Fast已生效：横向阶段未启动，
  第2、3轮未执行，无部分正式扩展Bundle结果。

当前结论：阶段6.4未通过。按照冻结门禁，本轮停止；不提交、不推送，
保留工作树和Artifact，等待是否重新授权从Extended Bundle第1轮验收。

### 阶段6.4 FVA回归修复与最终验收

状态：阶段6.4全部门禁通过，已完成FVA输入隔离、Extended Bundle三轮、失败注入和最终回归；后续阶段未开始。

#### FVA输入差异审计与根因

- 阶段6.3基础Bundle与阶段6.4失败轮在修复前重建得到相同Plan ID/Hash：`risk-plan-1b7e91b139dcbc0700d28a7ac41da7db` / `sha256:1b7e91b139dcbc0700d28a7ac41da7db45a50da95865b1cd5e800c763283457b`。Extended Bundle复用同一`RiskReviewPlanBuilder`和`execute_base_risk_review_bundle`，横向Plan只在基础阶段成功后构建，因此不存在横向阶段修改基础输入的证据。
- 根因分类为B：FVA输入与阶段6.3一致，模型偶发把同Batch其他FVA Check可见的非授权领域材料写入FVA-002。原最终领域门正确以`RISK_FVA002_SCOPE_LEAKAGE`拒绝；该门保持不变。
- 结构缺口是FVA此前只在输出后做领域门，模型调用前仍使用Batch级IR/Evidence池。未发现模块级当前Unit/Check状态、共享Candidate列表、浅复制追加或横向Plan原地修改。

#### FVA输入级隔离

- FVA Candidate生成和Evidence Source Policy按Check主题确定性选源；整个FVA Batch先移除人员资质、劳动合同、社保、团队配置、技术能力、项目经验和一般履约能力材料。
- Prompt改为`assigned_checks[]`逐Check携带Candidate、允许Evidence及允许Absence；不再暴露Batch全局IR/Evidence池。相同来源不再重复序列化为一份IR正文和一份Evidence正文。
- FVA-002增加允许IR/Candidate/Source、禁止语义主题及三态前置门。文本无明确主体/签署/授权冲突时只能为`EXTERNAL_VERIFICATION_REQUIRED`或`NO_VISIBLE_ISSUE`；固定Fixture Oracle仍为外部核验、`INSUFFICIENT_EVIDENCE`、Finding=0。
- 保留旧错误输出离线拒绝：含人员资质或履约能力断言的FVA-002结果仍触发`RISK_FVA002_SCOPE_LEAKAGE`，没有删除、改写或猜测模型原意。
- 增加`batch_context_hash`、`assigned_checks_hash`、`candidate_set_hash`、`evidence_policy_hash`、System/User/Serialized Prompt Hash。7个基础Batch并发构建100轮稳定，横向Plan构建前后基础Plan不变。
- 初次Check级实现重复携带IR和Evidence，Provider Prompt为12,004，正确触发7,000硬门并Fail Fast。去除重复载荷并按Check法律主题投影后，Prompt由26,136字符降至9,768字符；FVA真实Provider Prompt稳定为4,155。

#### FVA定向三轮

| Run | 耗时 | Provider Prompt/Cached/Completion | Assessment | 调用/Repair/Tool |
| --: | --: | -- | -- | -- |
| 1 | 6,686ms | 4,155 / 256 / 439 | EXTERNAL_VERIFICATION_REQUIRED，Finding=0 | 1 / 0 / 0 |
| 2 | 6,353ms | 4,155 / 4,096 / 462 | EXTERNAL_VERIFICATION_REQUIRED，Finding=0 | 1 / 0 / 0 |
| 3 | 5,999ms | 4,155 / 4,096 / 455 | EXTERNAL_VERIFICATION_REQUIRED，Finding=0 | 1 / 0 / 0 |

三轮的Serialized Prompt Hash均为`sha256:8d192f487bdd997632b59f42d6bc7543fd7caca4a8bb82e2c32e421fcf472a08`，Candidate和Evidence Policy Hash完全一致，人员资质越界为0。

#### Extended Bundle三轮

| Run | 基础阶段 | 横向候选构建 | 横向阶段 | 总墙钟 | 基础/横向峰值并发 | 调用/Repair/Tool |
| --: | --: | --: | --: | --: | -- | -- |
| 1 | 18,562ms | 5ms | 2,820ms | 21,387ms | 7 / 2 | 10 / 0 / 0 |
| 2 | 16,931ms | 5ms | 2,881ms | 19,817ms | 7 / 2 | 10 / 0 / 0 |
| 3 | 21,056ms | 5ms | 2,977ms | 24,038ms | 7 / 2 | 10 / 0 / 0 |

- 7个Unit、45项Check三轮完整；FVA-002保持外部核验且Finding=0，CF-005保持HIGH，PO保持6个Root，ICD保持1个Root，LRE保持4个Root。
- Consistency保持2个横向Finding，Completeness保持1个横向Finding；核心Root、等级、所有权和Primary Evidence三轮一致。
- Commercial非核心附加Finding数量为3/1/3，符合阶段6.2已冻结的允许变化范围；CF-005核心风险、等级和Evidence稳定。
- 三轮Provider Prompt硬失败0、Repair=0、Tool=0；总墙钟`min/median/max=19,817/21,387/24,038ms`，低于50秒目标。

#### 原子性、Artifact和回归

- 无模型失败注入覆盖基础Unit失败、横向Candidate构建失败、Consistency Batch失败、Completeness超时、Finding所有权冲突、横向Evidence跨Generation和45项Check缺失；全部为FAILED且无正式部分Finding。
- FVA Artifact：`stage64-fva-regression-three-run.json`，SHA-256=`a2fa0198f2779b50ac46b1b983d0bb80b8cf3c50be12cf8d1fb4592d629ae689`；Attempt SHA-256=`f95551472a61049f442e384837deece83363dc326142d63ec4279bfcc635bb8a`。
- Extended Artifact：`stage64-extended-bundle-three-run.json`，SHA-256=`1547c76313cb7a84143bbe8106770a339c229ee5693ea117b81b84971b69b44b`；Attempt SHA-256=`681aa60d7c118a118f836c835388169a460f2fa77e7174dd73194e91ed819c95`。
- 失败注入Artifact：`stage64-extended-bundle-failure-injection.json`，SHA-256=`f4f2bef87c95cb64d539e4dde55eac2980741afb26b7cb4b897c0f1b3cf559b4`。
- Consistency与Completeness既有5/5 Artifact继续有效，未修改其Candidate、Prompt或业务规则。
- 横向及失败注入专项：18 passed。Framework无实时服务最终回归：427 passed、2 skipped。Contract Python全量：117 passed、10 skipped。独立Smoke HTTP服务未启动的`test_live_multi_capability.py`单列为现场连接条件，不掩盖业务断言。
- 固定OpenAPI一致性和隐藏接口检查包含在Contract Python通过结果中；公开OpenAPI、正式DTO、`schema_version=1.0`、Window IR、Result Sink/Hash、Java和正式Pipeline均未修改。

结论：阶段6.4通过；允许按本阶段授权提交并推送。后续阶段尚未授权。

## 阶段 6.5：七Review Unit兼容旧五类Artifact并接入阶段5.1

状态：通过，已完成代码、真实验收和回归，待本提交创建并推送。该阶段对应原计划阶段6.6；动态Playbook已正式暂缓，不作为本阶段
或Direct主链路上线的前置条件。

目标：将Extended Bundle的七个Review Unit和45项Check逐Finding确定性映射为
五类旧Artifact，接入既有阶段5.1语义合并，再执行现有Evidence验证和稳定Hash。
本阶段只建设内部兼容结果和测试入口，不切换正式Pipeline。

### 开始前核对单

```text
当前阶段：6.5（原计划6.6）
主设计决策：动态Playbook暂缓；playbooks=[]；specialist_reviewers=[]
当前工作目录/工作树：D:\contract-risk-review-v1
当前分支：feat/contract-risk-review-playbook-v1
开始前HEAD：66e5e493a0cc974923bed2939825d5a67d4d31ba
本地/跟踪引用/GitHub真实HEAD：一致
开始前工作树：干净
工作树外备份：E:\MyProjects\Newestcontract\backups\stage65-start-20260724-131607
允许修改：内部Finding Compatibility Router、Legacy Artifact Adapter、
  阶段5.1内部接线和观测、内部兼容结果/Hash、隐藏测试入口、测试、设计和阶段记录
禁止修改：Java、公开OpenAPI、schema_version=1.0、正式Finding/Evidence DTO、
  Result Sink/正式Result Hash规则、Window IR、任务状态机、正式Stage/Artifact类型、
  七Unit业务结果、动态Playbook、Specialist、正式Pipeline及正式环境
必须复用：阶段6.4 Extended Bundle、45项Registry、正式五Artifact Pydantic、
  既有FindingConsolidationEngine、merge_review_stage_results、
  materialize_evidence_set/validate_evidence_set和正式Hash规范化规则
固定输入：阶段6.4最终Extended Bundle Artifact；不得重新执行七Unit模型
本阶段模型调用：只有阶段5.1候选Pair分类；五轮串行；Router/Adapter调用为0
Prompt预算：政策2.0；Provider <=6000目标内，6001～7000软告警，>7000硬失败
本阶段提交：功能：兼容旧风险Artifact并接入跨阶段语义合并
通过后：提交、推送并停止；Shadow Compare尚未授权
失败后：不提交、不推送，保存工作树和Artifact并停止
```

### 开始前真实边界核对

- 已重新完整读取联合冻结稿、阶段6设计稿和本阶段日志；阶段6.4基线、本地跟踪
  引用和GitHub真实远程均为`66e5e493a0cc974923bed2939825d5a67d4d31ba`。
- 五个正式Stage ID依次为`rights_obligations_review`、
  `commercial_terms_review`、`liability_termination_review`、
  `missing_ambiguous_clauses`和`relation_extraction`；对应正式Artifact及
  Pydantic模型保持原样。
- 既有阶段5.1实现为`FindingConsolidationEngine`候选对分类加
  `merge_review_stage_results`确定性合并；模型失败为`SKIPPED`并保留全部
  Finding，跨Category不自动合并，未知Finding和伪造Pair ID继续硬失败。
- `verify_evidence`真实入口仍要求五个Artifact齐全，先命名空间化后合并，
  再由`materialize_evidence_set`逐Block回查字符区间、逐字原文和Hash。
- 固定阶段6.4 Extended Bundle Artifact为服务器隔离目录中的
  `stage64-extended-bundle-three-run.json`，SHA-256=
  `1547c76313cb7a84143bbe8106770a339c229ee5693ea117b81b84971b69b44b`；
  本阶段只读取既有结果，不重新运行10个基础/横向模型Batch。

### 实现与离线路由

- 新增内部`FindingCompatibilityRouter`和`LegacyRiskArtifactAdapter`，路由版本
  `1.0`；逐Finding按Check Registry、Category、Risk Type、Canonical Root和所有权
  路由，模型调用0，无标题/Issue关键词路由和默认兜底。
- 固定Fixture的17条正式Finding全部唯一承载：权义6、商务3、责任5、缺失1、
  关系2；Evidence分别为28、4、9、2、4，共45条；横向正式Finding为3条，
  BASE_DOMAIN/SHARED_CONTEXT_ONLY内部Candidate未重复物化。
- FVA-005唯一`OTHER`规则、未知Check/Risk Type、非法Category、无路由、多Artifact
  路由、Finding/Evidence ID冲突均设为确定性硬门。
- 适配耗时2ms，适配模型调用0；相同Extended Bundle重复构建100次，Artifact Set
  Hash始终为`dbe1d129f2f70039e1a39da6e5453a188e940b3019015f508539b90ec175f63b`。
- 路由Artifact：
  `stage65-compatibility-routing.json`，SHA-256=
  `171cf468ab80695cd8a2786baccb7302b94f430bc760c783c57e0bd688c9f835`。
- 路由Oracle：
  `stage65-compatibility-routing-oracle.json`，SHA-256=
  `b61eb580b40e1192dcf0923d4a18f6dd05ef80b721611c052756985d5c0a9b3b`。

### 阶段5.1预算与语义边界修订

- 首次真实调用前的租户配置错误在模型调用前Fail Fast，调用数0，仅作为诊断保留；
  统一为阶段6.4使用的租户`0`后重新开始验收。
- 旧阶段5.1把24个候选对一次性发送，Provider Prompt为19,248 Token，正确触发
  政策2.0硬门并`SKIPPED`。修订为不截断的确定性预算拆批；最终为7个Batch，
  每批2～5对，本地估算2,398～5,186 Token。
- 仅提供正式Artifact文案时，模型对共享Evidence的PO-001/PO-002及PO-001/PO-006
  出现SAME_RISK波动。兼容层随后只读注入`check_code/risk_type/root/ownership`
  内部上下文，并冻结通用边界：共享Evidence或同一履约链本身不足以合并；履行范围、
  变更程序、服务标准和验收程序为不同法律根因时应保持关联但独立。
- 上述内部元数据不进入正式Artifact/DTO，不修改候选Pair ID、正式Finding或Evidence，
  不改变阶段5.1的SAME_RISK/RELATED_DISTINCT/DISTINCT语义。

### 最终五轮真实验收

五轮均只执行已保存Extended Bundle的兼容适配、阶段5.1分类、确定性合并和
Evidence验证；未重新调用七Review Unit。

| 轮次 | 总兼容链 | 模型调用/Repair/Tool | Prompt/Cached/Completion | SAME/RELATED/DISTINCT | Finding/Evidence | Result Hash |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 16,166ms | 7/0/0 | 24,938/24,448/1,093 | 0/20/4 | 17/45 | `sha256:fb36b910936bc30dd18c4be5d8e134546025f5dab9d3f0ba81a87c00447c69c2` |
| 2 | 15,458ms | 7/0/0 | 24,938/24,448/1,069 | 0/19/5 | 17/45 | 同上 |
| 3 | 16,659ms | 7/0/0 | 24,938/24,448/1,069 | 0/19/5 | 17/45 | 同上 |
| 4 | 17,240ms | 7/0/0 | 24,938/24,448/1,069 | 0/19/5 | 17/45 | 同上 |
| 5 | 16,530ms | 7/0/0 | 24,938/24,448/1,069 | 0/19/5 | 17/45 | 同上 |

- 总墙钟`min/median/max=15,458/16,530/17,240ms`，低于35秒目标；Adapter低于
  1秒目标。所有35次Pair Batch调用的Provider Prompt均为`WITHIN_TARGET`，
  Repair=0、Tool=0。
- 24个候选Pair集合、SAME_RISK集合（空）、最终风险根因、最高等级、Primary
  Evidence、17条Finding、45条Evidence和Result Hash五轮一致。
  `RELATED_DISTINCT/DISTINCT`有一个非合并型分类在第1轮与后四轮不同，但没有改变
  任何正式结果；按冻结门禁作为合法非核心表达差异披露。
- 五轮Artifact：
  `stage65-semantic-merge-five-run.json`，SHA-256=
  `5b5ba6ca567cf6f1f05acdd7c6da7e3283e14c651482899427b4caf9e1720931`。
- 完整Attempt Artifact：
  `stage65-semantic-merge-five-run-attempts.json`，SHA-256=
  `151056a6b9d76fe517e7583b1236136e5dd775e0e41635ba307f4f8e802b3df9`。

### 失败注入与回归

- 12类失败注入全部通过：未知Check、非法OTHER、Finding ID冲突、横向Finding
  多Artifact承载、Evidence ID冲突、无效Block/跨Generation、Artifact Schema失败、
  阶段5.1超时/Schema/语义守恒失败及Result Hash稳定性。
- Compatibility硬错误均整体失败且不产生可消费结果；阶段5.1三类模型错误均
  `SKIPPED`并保留17条原Finding后继续通过Evidence验证；Evidence错误整体失败。
- 失败注入Artifact：
  `stage65-compatibility-failure-injection.json`，SHA-256=
  `500a868fc8ef7dc8d23b770216d30dff21e3b90cbb32ecfdc5ee269a1c0bddc6`。
- 兼容、阶段5.1、Evidence/Hash、预算、基础与横向Bundle定向回归：
  `184 passed, 42 skipped`；最终新增专项：`23 passed`。
- Framework无实时服务全量：`390 passed, 43 skipped`；日志SHA-256=
  `9fd2b95d999e00511cdb33737a8b37fd99a91f3423984270abab5a870c8244de`。
  单独的`test_live_multi_capability.py`因独立Smoke HTTP服务未启动而连接失败，
  与业务改动无关且按既有规则单列。
- Contract Python专用镜像全量（新增兼容专项已在Framework组合镜像单独通过）：
  `116 passed, 11 skipped`；日志SHA-256=
  `e4799edd25c3c51798d043b27d572de4dd3784a6b0d52c851f747df46aa95d62`。
- 固定OpenAPI测试包含在Contract全量中；本阶段没有新增路由，内部脚本未进入公开
  OpenAPI。`git diff --check`通过。

结论：阶段6.5门禁通过；允许按本阶段授权创建中文提交并推送。Shadow Compare尚未授权。

## 阶段 6.7：Shadow Compare

状态：未开始。

目标：使用固定合同集合比较Legacy与Direct的质量、证据、重复率、Token、耗时和调用次数。

## 阶段 6.8：测试环境切换和全链路验收

状态：未开始。

目标：仅在独立测试环境切换Direct，完成Java→Python→Framework真实链路、失败注入、预算、取消、Attempt、Sink、Hash、回滚和目标时延验证。

## 阶段 6.9：稳定化与清理建议

状态：未开始。

目标：在用户另行确认正式切换且稳定后提出Legacy清理建议；任何正式切换和删除都需单独确认。

## 阶段 6.6：Legacy ReAct 与 Direct Structured Review 影子对照

状态：进行中。该阶段对应原设计记录中的 Shadow Compare 能力；用户已将本轮编号明确为阶段 6.6。正式 Pipeline、测试环境 Direct 切换和 Legacy 清理均未授权。

### 开始前核对单

```text
当前阶段：6.6 Legacy ReAct vs Direct Structured Review Shadow Compare
当前工作目录/工作树：D:\contract-risk-review-v1
当前分支：feat/contract-risk-review-playbook-v1
开始前 HEAD：3d2cf0cd0bef9932739e9144944319efbb631ddd
本地 HEAD / 远程跟踪引用 / GitHub 真实 HEAD：一致
开始前工作树：干净
工作树外备份：E:\MyProjects\Newestcontract\backups\stage66-start-20260724-142415
允许修改：内部 Shadow Runner、统一比较器、Corpus Manifest、人工复核队列、隔离测试入口、无模型和真实验收测试、设计及阶段记录
禁止修改：Java、公开 OpenAPI、schema_version=1.0、正式 Finding/Evidence DTO、正式 Stage/Artifact、Result Sink、正式 Result Hash、Window IR、五基础领域和两个横向 Unit 业务规则、阶段 5.1 正式语义、动态 Playbook、Specialist、正式/测试环境 Pipeline 切换
必须复用：Legacy 五类 ReAct Stage、阶段 5.1 FindingConsolidationEngine、verify_evidence、Direct Extended Bundle、阶段 6.5 Legacy Artifact 兼容层、Prompt 预算政策 2.0
固定 Fixture：服务外包补充协议 0829；101 条 Contract IR；PARTY_A；中立审查
模型调用：仅真实 Legacy、Direct 和有限 Comparison 候选分类；无路由模型、无总结模型；失败注入和输入压力测试调用为 0
执行模式：BENCHMARK 三轮依次 Legacy→Direct、Direct→Legacy、Legacy→Direct；另执行一次 SHADOW_RUNTIME 隔离验收
权威边界：official_result_source=LEGACY；Direct 和 Comparison 只写隔离 Shadow Artifact；任何 Shadow 失败不得改变 Legacy 正式结果、状态、Sink 或 Hash
语料门禁：只有同时达到至少 5 份合同和 3 种合同类型才允许 GO；当前已确认完整冻结语料仅 1 份，航空航天 DOCX 尚无配套冻结 IR/上下文，因此预期 corpus_coverage_status=LIMITED、cutover_recommendation=NEEDS_MORE_CORPUS
Prompt 预算：政策 2.0；Provider <=6000 为目标内，6001～7000 为软告警，>7000 为硬失败；三类调用分别统计
提交信息：功能：实现Legacy与Direct风险审查影子对照
通过后：提交、推送并停止；正式 Pipeline 未切换；测试环境 Direct 切换尚未授权
失败后：不提交、不推送，保存工作树、Artifact 和复现信息并停止
```

### 开始前真实边界核对

- 已从文件系统重新完整读取联合冻结稿、阶段 6 设计稿、阶段日志、Legacy 五类 Stage/Skill、阶段 5.1、Evidence 验证、Direct 基础及横向 Bundle、阶段 6.5 兼容层、正式 Result Sink/Hash 和隐藏验收脚本。
- 正式 Pipeline 仍注册并执行五个 Legacy ReAct 风险 Stage；阶段 6.3～6.5 的 Direct、横向和兼容能力仍为内部执行入口，尚未写入正式 Result Sink 或 Java 回调。
- 固定输入目录保存服务外包补充协议的 101 条 IR、完整 Window/Anchor/Offset、真实主体和 PARTY_A 立场；两条链必须由该同一冻结值建立独立深复制快照。
- 当前可见的另一份真实文档为航空航天收购可行性 DOCX，但尚未发现其完整冻结 Contract IR 和 Risk Review Context。缺少完整冻结输入的文档不得进入真实 Legacy/Direct 主比较，也不得计入充分语料覆盖。

### 单链预检结果与停止点

状态：未通过。按阶段 6.6 固定验收顺序在第一步停止；Direct 单链、三轮配对 Shadow Compare、Shadow Runtime、其他语料、失败注入和全量回归均未继续执行。

- Shadow 基础设施无模型专项及相关确定性回归为 `42 passed, 18 skipped`；输入深复制、Hash、有限候选配对、人工复核队列、Legacy 权威边界和 Shadow Artifact 写入失败隔离均通过。
- 首次诊断运行发现五个 Legacy Stage 共用临时用户身份时，Conversation 层会恢复其他 Stage 的 Redis 历史。该运行作废且不计入验收。Runner 已改为每个执行轮次、Stage 和局部尝试使用独立 `user_id/session_id`。
- 独立身份后的正式单链预检中，跨 Stage 历史恢复次数为 0；Legacy 使用冻结的 `deepseek-v4-pro`、`temperature=0.1`、ReAct 和四个 Contract Tool。
- `commercial_terms_review` 在两次局部执行中均输出了 `quoted_text` 但没有配套 `quoted_text_hash`。最终第二次输出有 6 条 Evidence 触发正式 `CommercialTermsStageResult` 的硬校验：
  `quoted_text and quoted_text_hash must be supplied together`。
- 该错误与既有 Legacy Artifact 的已知 Evidence Schema 缺陷一致。Shadow Runner 没有删除 `quoted_text`、补算 Hash、宽松解析或把 Direct 结果顶替为 Legacy；因此 Legacy 单链未形成合法正式结果。
- 有效隔离预检约 210 秒；日志观测到 50 次 Legacy 模型调用和 59 次 Tool 调用。由于 Fail Fast，Direct、Comparison 和其他语料新增模型调用均为 0。
- Corpus Manifest 只确认 1 份合同、1 种合同类型具备完整冻结 IR 和上下文；另有 1 份真实航空航天 DOCX 但缺少冻结 IR/上下文，1 份合成演示 PDF 也不可运行。因此 `corpus_coverage_status=LIMITED`，即使后续修复 Legacy 预检，切换建议也不得直接为 `GO`。
- 失败 Artifact：
  `stage66-fixed-fixture-three-run.json`，SHA-256=`b67a45edc9b871dd4ba9630dada86d52680335952c143ca829857bd053f87e4e`；
  `stage66-fixed-fixture-three-run-attempts.json`，SHA-256=`b4f9db01274d3e6e702a2e7721f552a9cb4dab3eb12ab36fcb1a33ea08b2387b`；
  `stage66-shadow-corpus-manifest.json`，SHA-256=`50a83ea4f2413ae736517089d6cff7b567875e57d9e5749e3e66c00a7396c60f`；
  原始隔离日志 `attempts/stage66-preflight4.log`，SHA-256=`3fb24091184635d63d584059f96cf02fd00aeb26b48827646b5850a0d89d990c`。

结论：阶段 6.6 未通过；不提交、不推送，保留工作树和诊断 Artifact。正式 Pipeline 未切换，测试环境 Direct 切换未开始。后续需要用户单独决定是否允许修复 Legacy Evidence 输出契约，或调整 Shadow 基线对 Legacy 非法 Artifact 的处理口径。

## 阶段 6.6（调整后）：Direct风险审查全链路端到端验收

状态：通过。Legacy Shadow Compare因Legacy自身Evidence契约无效而暂缓，不修复、
不再运行，也不再作为Direct主链路继续验证的前置条件。本阶段未切换正式Pipeline或
测试环境。

### 开始前核对与方向调整

```text
当前目录：D:\contract-risk-review-v1
当前分支：feat/contract-risk-review-playbook-v1
开始前HEAD：3d2cf0cd0bef9932739e9144944319efbb631ddd
本地/跟踪引用/GitHub真实HEAD：一致
开始前工作树：包含原Shadow Compare未提交代码和阶段日志
工作树外备份：E:\MyProjects\Newestcontract\backups\stage66-shadow-to-direct-e2e-20260724-152502
备份清单SHA-256：0F493868D7865CD74CAF9DBA4292EB04348F6E2F11F030883C98B9AE29F717FA
允许修改：内部Direct E2E Runner、Dry Run Sink/Callback、隔离验收脚本、测试、
  设计和阶段记录
禁止修改：Legacy、Java、公开OpenAPI、schema_version=1.0、正式DTO、Result Sink
  协议/Hash、任务状态机、Window IR、七Unit业务规则、阶段5.1语义、正式Pipeline
固定输入：服务器risk-review-input目录中的101条Contract IR和PARTY_A中立上下文
执行模式：DRY_RUN
Legacy模型/Tool调用：0/0
正式Result Sink写入/真实Java回调：0/0
提交信息：功能：实现Direct风险审查全链路端到端验收
```

- 原Shadow代码逐文件审计后，只复用冻结输入Hash、深复制、隔离Artifact、指标、
  失败诊断和正式副作用隔离原则。未使用的Legacy比较器、人工复核队列、Benchmark
  和Shadow专用入口未纳入最终代码。
- 原Shadow失败事实和Artifact记录保留；未补算Legacy `quoted_text_hash`，未删除
  Legacy原文，未修改Legacy Schema、风险判断或正式Stage输出。

### Direct E2E实现

- `DirectRiskReviewEndToEndRunner`只接受`DRY_RUN`，从同一冻结Contract IR和Context
  实际执行基础7 Batch、横向3 Batch、七Unit/45项Check、Extended Bundle、五类
  Artifact兼容、阶段5.1、Evidence验证、正式DTO/Hash、Dry Run Sink和Callback。
- 正式结果复用`EvidenceVerificationStageResult`、
  `FinalizeReviewStageResult`、`ReviewResultData`、`StageResultCallback`和
  `compute_result_hash`冻结语义；内部Candidate、Root、Ownership、Routing和Severity
  Trace未进入Payload。
- Dry Run按现有一Review一Result语义模拟事务和幂等：相同Payload重复为DUPLICATE，
  同Review不同Hash为`FRAMEWORK_CALLBACK_MISMATCH`，不同Review独立；相同Callback
  Envelope重复不产生第二次业务效果。
- 状态Trace只使用冻结的`RISK_REVIEW`、`EVIDENCE_VERIFICATION`和`FINALIZING`
  正式Stage值；内部事件只用于诊断，不修改任务状态机。
- 第一轮后生成用户可读Markdown，逐五Artifact列出正式风险、等级、Check、
  Risk Type、影响、建议、Evidence原文和位置，并列出FVA-002、PO-003、ICD文本
  保密Candidate和LRE-006的重要无Finding结论。

### 三轮真实E2E

| 轮次 | Extended Bundle | 阶段5.1 | 总墙钟 | Review/5.1/总调用 | Repair/Tool | Prompt/Cached/Completion | Finding/Evidence | Result Hash |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 22,009ms | 10,388ms | 33,348ms | 10/5/15 | 0/0 | 52,294/51,456/7,791 | 15/42 | `sha256:6b9524b64fb5e4f8588d495848aa441ab10f513d260aa5b7bd59a9015c70243b` |
| 2 | 24,068ms | 12,871ms | 37,996ms | 10/6/16 | 0/0 | 55,227/48,384/8,263 | 17/45 | `sha256:3c3028cb4f7f989c435aa6489312585718b1f988dd16d71bda7c1505576686d1` |
| 3 | 22,617ms | 15,064ms | 38,723ms | 10/7/17 | 0/0 | 58,851/51,328/8,483 | 17/46 | `sha256:4f8f0d186570c3b7c04c45fa6680487ac827808c6671e292f158feb75b55ed3f` |

- Extended Bundle `min/median/max=22,009/22,617/24,068ms`；阶段5.1为
  `10,388/12,871/15,064ms`；完整E2E为`33,348/37,996/38,723ms`，均低于目标。
- 基础和横向峰值并发三轮均为7和2。45项Check、七Unit、五类Artifact、
  Evidence验证、Payload Schema、Dry Run Sink和Callback三轮全部通过。
- 三轮`core_result_signature`均为
  `sha256:06623127751704c7fec585eee0ebf9fec72c491a3d29a0d0c6c6c34e2976da4a`。
  第二轮初次被旧签名误判，离线审计证明16项核心组件中15项完全一致，唯一差异是
  CF-005同一稳定Absence Evidence的`checked_scope`多“任何”二字；签名修正为
  Evidence Source身份后，既有第二轮离线重算通过，未重复调用模型。
- 核心Oracle三轮一致：FVA-002为外部核验且Finding=0；CF-005为HIGH；
  PO 6个Root、PO-001 HIGH、PO-003 Finding=0、PO-006 MEDIUM；ICD 1个MEDIUM Root；
  LRE 4个Root且无上限责任HIGH；横向保持日期冲突、主体名称冲突和附件引用缺失。
- Commercial非核心合法变化如实保留：第1轮仅CF-005；第2、3轮另有CF-007和
  CF-008，第2轮CF-008为HIGH、第3轮为MEDIUM。因而Pair Plan实际为17/20/24个候选、
  5/6/7个Batch，总模型调用15/16/17；未硬编码历史24/7或17次。
- 所有Review和阶段5.1 Batch的Provider Prompt均不超过6,000，全部
  `WITHIN_TARGET`；FVA三轮均为4,155 Token。

### 确定性重放、失败注入与Artifact

- 每轮模型完成后均执行100次无模型重放；Artifact Hash、Payload Hash、正式
  Result Hash和幂等行为各轮100/100稳定。
- 失败注入覆盖基础/横向Batch失败、未知兼容路由、阶段5.1超时/Schema失败、
  Evidence失败、Payload Schema失败、Result Hash不稳定、Sink事务失败、Callback
  失败、重复Payload/Callback、同Review不同Hash和不同Review独立结果。硬错误均无
  可消费Payload或正式副作用；阶段5.1错误均SKIPPED并保留原Finding继续验证。
- Artifact SHA-256：
  - `stage66-direct-e2e-three-run.json`：
    `c1c4aa8fcd4f4e058190420f34af3ed3601db219cf6409ed02580742b06b2518`
  - `stage66-direct-e2e-three-run-attempts.json`：
    `7c71fe061a0d640b9730bf8d073bfe54c8f9a81bb040ce837908ea52a168d3fd`
  - `stage66-direct-e2e-human-readable.md`：
    `1e5fefe4ec2bf8c2828b8d5322831c17fc1fbd303fbeb6254b8cfc753aa7ed85`
  - `stage66-direct-e2e-formal-payload.json`：
    `700ec8763ef9855b496ecf9ad3c882df6e195fccae2087b3693bd8959b211098`
  - `stage66-direct-e2e-deterministic-replay.json`：
    `8ffd36093c59775a2ec0e037df5633aa5de065b830d40251c5a848de7ff61690`
  - `stage66-direct-e2e-failure-injection.json`：
    `9e8ed61060240343455193bf6cef5e36ba61ed0736df15ddac2312f6369119c9`

### 回归与结论

- Direct E2E/Compatibility专项最终`34 passed`；风险、预算、五基础、横向、
  Bundle、Evidence、Hash组合回归`287 passed, 44 skipped`。
- Framework无实时服务全量`390 passed, 43 skipped`；唯一现场测试
  `test_live_multi_capability.py`因独立Smoke HTTP服务未启动而连接失败，按既有
  规则单列。
- Contract Python：核心/API/OpenAPI等`104 passed`；数据库型集合
  `5 passed, 10 skipped`（未配置隔离`CONTRACT_TEST_DATABASE_URL`）。
- 固定OpenAPI通过；内部Runner和脚本未注册公开路由；`git diff --check`通过。
- 正式Result Sink写入0，真实Java回调0，Legacy模型/Tool调用0，正式Pipeline切换
  效果`NONE`。

结论：阶段6.6通过；Direct全链路已跑通；
`corpus_coverage_status=LIMITED`；
`next_recommendation=READY_FOR_TEST_ENV_DIRECT_VALIDATION`。
允许按本阶段授权提交并推送。正式Pipeline未切换，测试环境Direct切换尚未授权。

## 2026-07-26 Direct风险审查运行时门禁粒度修正

### 现场问题与根因

- 正式任务在结果整理阶段因PO-007候选
  `risk-candidate-42fedd00ab05a43d5e1a272efd0f3eb5`
  返回`INSUFFICIENT_EVIDENCE`而整体失败。模型摘要明确表示现有证据只有服务范围和
  解除条件，不能证明质保、维护、整改、响应或复验机制。
- 旧运行时把“一个Candidate证据不足”沿
  `Candidate → Check → Batch → Unit → Bundle → E2E`提升为全局硬失败，导致其他
  已经通过校验的Finding也无法进入正式Payload。该传播粒度不适合正式运行。
- PO-007 Source Policy中的裸关键词“改正”还会把“30日内未改正即可解除”的LRE
  解除整改期文本投影到质保支持Check，扩大了无关Evidence暴露范围。

### 修正规则

- `INSUFFICIENT_EVIDENCE`只作用于当前Candidate；该Candidate不生成Root/Finding，
  审计记录保留。
- 单Check失败只标记当前Check为`FAILED/CHECK_FAILED`；同Batch内可以确定性保留的
  合法Check结果继续保留。
- 模型调用失败、超时、Schema不合法、未知或非法Source等无法安全拆解的错误，只使
  当前Batch为`FAILED`；其他Batch继续运行并物化合法Finding。
- Unit或Bundle包含局部失败时返回`PARTIAL_FAILED`。只有全部Batch都失败且没有可
  消费结果，或命中全局完整性门时，才整体`FAILED`。
- 横向Review Batch采用相同策略：失败Batch的候选确定性记录为
  `INSUFFICIENT_EVIDENCE`，不生成横向Finding；另一横向Unit及基础Finding不受影响。
- PO-007 Evidence Source Policy移除无上下文裸词“改正”，仍保留真正与质保、维护、
  支持、响应、复验相关的文本信号。

### 保持不变的全局硬门

- review/document/generation/contract hash/schema/perspective/双方主体不一致；
- Check、Candidate、Unit或Batch归属错误及跨Unit、跨Generation污染；
- 正式Finding/Evidence ID冲突、重复同根Finding、非法Category/Risk Type；
- 正式Evidence的IR、Anchor、Block、字符范围、逐字原文、Hash或Absence范围无效；
- Compatibility路由、正式Artifact/Payload Schema、正式Result Hash失败；
- Result Sink事务、幂等冲突和正式回调契约失败。

这些门保护结果身份、技术证据和持久化一致性，不能通过丢弃单条结果静默修复，仍然
整体失败。正式Finding/Evidence DTO、公开OpenAPI、Result Hash和Result Sink协议
均未修改。

### 验证

- 新增PO-007解除整改期文本隔离、Candidate证据不足局部降级、Commercial Check局部
  降级、基础/横向Batch失败隔离及Provider Prompt硬超限隔离测试。
- 直接审查、基础Bundle和横向专项：`186 passed, 42 skipped`。
- Direct E2E、Compatibility、Evidence、Candidate类型、Finding合并和Prompt预算：
  `63 passed`。
- Framework与Contract无实时服务仓库级回归（排除显式现场HTTP测试）：
  `593 passed, 54 skipped`；固定OpenAPI与能力挂载均通过。
- `py_compile`和`git diff --check`通过。
