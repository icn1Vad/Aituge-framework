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

状态：未通过；真实模型在第1次验收的唯一修复后仍未满足严格Schema，已按硬失败门禁停止。未继续剩余4次，不提交、不推送、不进入阶段6.3。

- 输入输出消歧：Direct Reviewer输入改为`assigned_check_specs`，模型输出仍唯一为`check_results`；Plan Context内部原`check_specs`不变，由Adapter显式映射。
- 受限规范化：只允许在顶层唯一字段为`checks`、八个单项均严格合法、Check完整且唯一、改名后整体严格合法时执行`TOP_LEVEL_CHECKS_TO_CHECK_RESULTS`；不修改任何业务字段，不计模型修复。
- 修复安全：修复请求携带完整首轮原文、精确Pydantic错误、允许字段和禁止改动；不再提供八项全空模板。修复前后确定性比较既有Check判断、Finding、风险根因/等级和Evidence；语义变化直接失败。
- 原始诊断：`ReviewUnitResult`及失败Artifact保存每次模型原始Content、SHA-256、精确错误、规范化、语义门禁、Token、TTFT、模型耗时、Trace和Provider Request ID。
- CF-005候选：仅基于当前Plan的PAYMENT/AMOUNT/DATE/DELIVERY/ACCEPTANCE/OBLIGATION/LIABILITY/TERMINATION及Anchor生成，不自动创建Finding。固定Fixture确定性识别`substantial_prepayment=true`、`payment_before_performance=true`、无已识别保障，候选IR=`I008/I038/I058`、Evidence=`A026`。
- 自动测试：隔离服务器容器中`43 passed`；覆盖字段改名、严格规范化拒绝边界、修复语义守恒、`candidate_ir`额外字段拒绝、CF-005候选、Fixture、LlmRuntime兼容、Tool=0、`temperature=0`、`thinking=false`。固定Fixture System+User Prompt本地Tokenizer低于6,000 Token。
- 预验收失败：首次真实尝试的首轮及修复均额外输出八个`candidate_ir`字段，被严格Pydantic拒绝；该版本尚未具备失败原文持久化，只有日志，无可恢复的完整原文。日志=`stage62-commercial-schema-cf005-5.log`，SHA-256=`f1c03f3c6d915b69b34bc355d9b1cfb3330843cb88a147602bb88a20627a7ec2`。随后只补充精确允许字段和失败Artifact，不放宽Schema。
- 正式重验第1次首轮：顶层正确为`check_results`，覆盖CF-001～CF-008；`schema_normalization_applied=false`；真实模型调用1次，`16,867ms`，TTFT=`1,921ms`，Prompt=`5,674`、Cached=`0`、Completion=`1,235`、Total=`6,909` Token。
- 首轮质量：CF-003、CF-004均稳定判定无对我方不利的实质风险；CF-005明确输出HIGH预付款保障风险，包含付款原文`I058/A026`和保障缺失ABSENCE，未被清空。首轮失败原因是八个`reason_code=null`，且非CF-005的两个候选列表字段为`null`而非空数组。
- 唯一修复：模型保留全部八个Check、CF-005 Finding和Evidence，并把候选列表改为空数组；但八个`reason_code`变成空字符串，仍违反`min_length=1`。修复调用`11,597ms`，TTFT=`959ms`，Prompt=`9,749`、Cached=`6,784`、Completion=`1,213`、Total=`10,962` Token。
- 修复语义门禁：未执行到接受门，因为修复结果先被严格Pydantic拒绝；从已保存原文可见CF-005未被删除，但不能将Schema非法结果声明为语义门禁通过。
- 本次总耗时=`28,507ms`，模型调用=2，模型修复=1，Tool=0，低于50秒修复性能门和60秒硬上限；性能通过不抵消Schema失败。
- 新Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage62-commercial-schema-cf005-5-v2.json`，57,987字节，SHA-256=`96320ece8925aa4b6c378e3846a41ae0b968eb391018960f2db0927f1348a1fd`。
- 新日志：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage62-commercial-schema-cf005-5-v2.log`，24,370字节，SHA-256=`35919bf3d110ecac43fec29ee189e29a73751d8a290be3c38bce3c480751921b`。
- 门禁结论：只完成1/5正式重验即发生“唯一修复后Schema仍非法”的硬失败；不得继续其余4次，不执行全量Framework/Contract/OpenAPI回归，不创建提交。

结论：字段名冲突、旧修复清空风险和CF-005漏判已有实质改善，但首轮`reason_code`及候选空值Schema仍不稳定。阶段6.2继续未通过，禁止进入阶段6.3。

### 阶段6.2 reason_code职责修订与最终验收

状态：通过；已完成本阶段实现、五次真实Direct验收和回归门禁。完成后停止，不自动进入阶段6.3。

- 本轮授权文件：`D:\codex\.codex\attachments\c3eee349-7656-4484-be7e-df386731f07c\pasted-text.txt`；SHA-256=`dd608a639374e1cdd9433add4042108017b719d62ebba1df7a1789cb9a8a9667`。
- Schema职责：模型Raw层只负责`check_code/status/decision_note/findings/evidence`；兼容接收`reason_code: str|null`但始终忽略。Python在Raw Pydantic、Check覆盖及Finding/Evidence语义校验后生成Final封闭枚举。
- 确定性映射：有Finding的`REVIEWED -> RISK_IDENTIFIED`；无Finding的`REVIEWED -> NO_RISK_IDENTIFIED`；`NOT_APPLICABLE -> NOT_APPLICABLE`；`FAILED -> CHECK_FAILED`；候选存在但Evidence不足时使用`INSUFFICIENT_EVIDENCE`。
- Final模型继续严格且拒绝`null`和空字符串；补全不改变Finding、风险等级、Evidence、Check状态或`decision_note`。缺失、空值或任意模型`reason_code`不触发修复。
- 可观测性：新增`reason_code_enrichment_count`、`reason_code_rule_version=1.0`、`ignored_model_reason_code_count`；A/B Artifact同时保存Final reason code和decision note。
- 自动测试：Direct、Fixture和LlmRuntime兼容定向测试`50 passed`；覆盖Raw空值/任意值、五种确定性映射、Final拒绝非法值、补全语义守恒、仅reason问题不修复、Tool=0、`temperature=0`、`thinking=false`和旧`complete()`兼容。
- 固定输入保持101项IR、93个Block和12个Window；没有重新解析合同或重新生成IR。本轮只执行同一`commercial_financial` Direct Reviewer五次，没有再次调用Legacy。

五次真实Direct结果：

| 次数 | 总耗时 | TTFT | Prompt/Cached/Completion Token | 模型/修复/Tool | Finding/Evidence | CF-005 | CF-003/004 |
|---:|---:|---:|---|---|---|---|---|
| 1 | 24,249ms | 2,899ms | 5,677/1,536/1,671 | 1/0/0 | 3/6 | HIGH，RISK_IDENTIFIED | 均NO_RISK_IDENTIFIED |
| 2 | 14,301ms | 704ms | 5,677/5,632/1,060 | 1/0/0 | 1/2 | HIGH，RISK_IDENTIFIED | 均NO_RISK_IDENTIFIED |
| 3 | 14,849ms | 673ms | 5,677/5,632/1,080 | 1/0/0 | 1/2 | HIGH，RISK_IDENTIFIED | 均NO_RISK_IDENTIFIED |
| 4 | 19,492ms | 845ms | 5,677/5,632/1,480 | 1/0/0 | 3/4 | HIGH，RISK_IDENTIFIED | 均NO_RISK_IDENTIFIED |
| 5 | 14,780ms | 782ms | 5,677/5,632/1,076 | 1/0/0 | 1/2 | HIGH，RISK_IDENTIFIED | 均NO_RISK_IDENTIFIED |

- 性能：min=`14,301ms`、median=`14,849ms`、max=`24,249ms`，五次均无修复并低于30秒正常路径目标。
- reason code：五次均完整补全8项，规则版本均为`1.0`；模型按Prompt省略该字段，`ignored_model_reason_code_count=0`；Final没有空值。
- 质量：CF-001～CF-008均完整覆盖；CF-005五次均识别“全额或绝大部分预付款且缺少履约保障”，等级HIGH，Evidence同时包含付款原文和ABSENCE；CF-003/004核心判断均稳定为无实质不利风险。
- Evidence：所有正式Evidence均确定性映射到既有IR、Anchor、Block和字符区间；有效率100%，无依据Finding为0。
- CF-007/008在部分运行中产生有原文依据的交付/验收Finding，其余运行判无风险；这不影响本阶段已冻结的CF-003/004稳定门和CF-005 5/5门，且没有无依据结果。
- A/B Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage62-commercial-reason-code-5.json`，145,948字节，SHA-256=`facbc9d7ba899b974fdd983a37995ac7d7127ed750c8e9a933bc89d90469ff98`。
- 运行日志：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage62-commercial-reason-code-5.log`，49,472字节，SHA-256=`ce1d791f83967bbcca0aee81c49fc74d292145f0d43e7c28f4c1b6fae9d77448`。
- Framework回归：完整集合包含一个依赖独立Smoke HTTP服务的既有`test_live_multi_capability.py`，该服务未在阶段6.2容器中启动时返回Connection refused；排除该外部现场用例后，代码回归`244 passed`。日志SHA-256=`3fb01aa3c685891533fbc2d953d9d7740a09ceea0e2d087b9d6b42c4c1eeb5e5`。
- Contract Python全量回归：复用服务器现成隔离测试镜像运行，`117 passed, 10 skipped`；日志SHA-256=`e878628cf8899cd9511a61a48ada60bda6032b596a0166ea2d919bd3d71ac046`。
- 固定OpenAPI：运行时与`contract-agent-openapi-v1.json`逐对象一致，显式测试`7 passed`；隐藏Risk Plan接口仍不出现在公开OpenAPI。日志SHA-256=`4ae14dbeffefbba53d44715f9f2ea067aaa2085312a70a4adf93619260f07e6c`。
- 没有修改Java、公开Java–Python DTO、`schema_version=1.0`、数据库、Compose、Window IR、其他四个Reviewer或正式容器；没有合并`main/proof`。

结论：阶段6.2质量、性能、Schema、Evidence、回归和协议门禁全部通过。当前只允许提交并推送阶段6.2功能分支；完成后必须停止，阶段6.3仍为未开始，须等待用户另行授权。

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
