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

状态：准备中；已完成开始前复读与边界核验，尚未修改阶段6.3业务代码。

目标：扩展到五个基础Unit，完成Direct Structured Review、受控并发、严格Check覆盖和Bundle校验。

### 阶段6.3开始前核对单

- 当前阶段：6.3；只实现五个基础Direct Reviewer、七个基础Batch及`execute_base_risk_review_bundle`内部并行执行；完成后停止，不进入阶段6.4。
- 本轮授权文件：`D:\codex\.codex\attachments\4793ef5d-32c2-44b2-a664-7db65e36428a\pasted-text.txt`；16,174字节；SHA-256=`1c7131774d9dd2a5280970af4b1cbfb1e3304e72fa3cc20c441716cabce4ee93`；最后修改时间=`2026-07-23T12:49:37+08:00`；已完整读取。
- 联合冻结稿：`E:\MyProjects\Newestcontract\合同审查一期 Java–Python–Framework 联合技术方案 v1.0（冻结稿）.md`；60,026字节；SHA-256=`0e1de5f8c7e9d97fdfe17b202c92f084e07ac0c7f17702c9942337504129bbe0`；已重新完整读取，公开Java–Python协议、状态机、Finding/Evidence、Result Hash和Framework边界不变。
- 阶段6设计稿：`docs/contract_risk_review_design.md`；29,624字节；SHA-256=`b0589544ad77df61877983914c682306d9a5ec7c4ee1c82dea4789556a5850a8`；已重新完整读取。
- 阶段日志：开始前38,453字节；SHA-256=`d0bbb1060f951cb0c694af529bb4ebdbabd8ae325f387f814bebf0bd5a66b4ff`；已重新完整读取。
- Window阶段日志：`contract_ir_window_stage_log.md`；40,292字节；SHA-256=`ddabfbb6c723e3eae94608cfdd312f5f8a22eeed017fb57cc9f73b3843e47d2e`；已复读阶段5、5.1和最终基线。固定结果仍为93个Block、12个Window、101项IR；阶段5.1只做安全重复Finding合并，不改业务结论。
- 工作目录/工作树：`D:\contract-risk-review-v1`；分支=`feat/contract-risk-review-playbook-v1`；HEAD=`0dfc336b98850783e0ae5783af25e805799eeba7`；远程同名分支HEAD一致；开始前工作树干净。
- 当前阶段允许修改：四个新增领域Reviewer、共享Direct Review执行内核、领域Prompt和严格Raw/Final Pydantic模型、确定性候选生成、七Batch确定性归并、基础Bundle并发执行、内部指标、隔离测试脚本、测试、设计稿与阶段日志。
- 当前阶段禁止修改：两个横向Reviewer、Specialist、Playbook扩展示例、旧阶段Artifact Adapter、阶段5.1正式接线、正式Pipeline与默认引擎、Java、公开OpenAPI、`schema_version=1.0`、公开DTO、Result Sink、Window IR、数据库、正式容器、`main/proof`及阶段6.4以后代码。
- 必须复用：阶段6.1的Playbook Registry、Plan Builder、Plan Hash、Context投影和隐藏Plan接口；阶段6.2的Tool=0、`temperature=0`、`thinking=false`、JSON Object、严格Raw/Final、确定性`reason_code`、一次局部Schema修复、Evidence映射、CF-005候选与证据门禁。
- 已复读阶段6.1模型、Registry、Plan Builder及隐藏内部服务；已复读阶段6.2 Reviewer、测试和A/B脚本；已复读五个Legacy风险Skill，仅作为召回与边界参考，不恢复ReAct或Tool取数。
- 历史Artifact已核验：阶段5结果19条Finding/27条Evidence；阶段5.1从19条安全合并为15条；`stage51-source-review-artifacts.json` SHA-256=`33eb9732aa20e1ad9736763dc6db7580f017b56fae6ce03c2a30b42ebc3d6d8b`；阶段6.2最终Artifact SHA-256=`facbc9d7ba899b974fdd983a37995ac7d7127ed750c8e9a933bc89d90469ff98`。
- 固定Fixture：服务器`/home/aituge/workspace/contract-review-dev/test-artifacts/risk-review-input`；只读复用，不重新解析合同、不重新生成IR。
- 本阶段模型调用：实现和无模型测试期间为0；四个新领域各通过无模型门禁后，按同一Fixture分别真实运行3次；全部通过后才运行五领域Bundle 3次。
- 最大模型调用：单领域按Batch数每次正常1或2次；基础Bundle正常每次7次，任一Batch最多一次局部修复。禁止Legacy重跑、横向模型调用和Specialist调用。
- 配置与容器：不得修改正式配置；允许在既有隔离服务器工作区创建阶段6.3测试副本、重启隔离测试容器或在其内挂载新代码；不得操作正式环境。
- 单元测试门禁：五领域34个Check完整且唯一；FVA-005唯一`OTHER`规则；未知/重复/缺失Check、Evidence错误、跨Unit污染、任一Batch失败、修复语义变化、并发异常均硬失败；Commercial阶段6.2回归必须全部通过。
- 质量门禁：四个新领域分别3次真实运行，Check覆盖100%、Evidence有效率100%、无依据Finding=0、Tool=0、正常每Batch一次调用；然后Bundle 3次覆盖34个Check、7个Batch和5个Unit，峰值并发目标7，核心风险语义稳定。
- 性能门禁：新领域无修复目标不超过30秒、一次局部修复允许不超过50秒、单Unit硬上限60秒；Bundle目标不超过35秒、允许不超过45秒、硬上限60秒；只报告三次min/median/max。
- 提交信息：全部门禁通过后仅允许中文提交`功能：实现合同五领域并行风险审查`并推送当前功能分支。
- 通过后允许进入的下一阶段：无；阶段6.3完成后必须停止，阶段6.4需用户另行授权。
- 失败后必须停止的位置：任一无模型、真实质量、性能、回归、OpenAPI或工作树门禁失败即停止；不提交、不推送，保留Artifact和复现信息。

### 阶段6.3第一轮实现与质量门失败记录

状态：未通过；四个新增领域真实验收在`formation_validity_authority`第1次运行触发质量硬失败，已停止。未运行其余领域，不运行基础Bundle，不提交、不推送、不进入阶段6.4。

- 实现范围：增加四领域共享Direct Review内核、严格Generic Request/Raw/Final模型、领域边界和逐Check判定规则、确定性候选、Evidence映射、七Batch并行基础Bundle、Unit确定性归并、34 Check完整性和并发指标；阶段6.2商务财务Reviewer及CF-005专用门禁保持原执行逻辑。
- 通用结果模型：阶段6.2的内部`FindingDraft`、`CheckCoverageResult`、`LlmCallMetric`和`ReviewUnitResult`只拓宽为五基础领域可承载类型；未修改Java、公开DTO、公开OpenAPI、`schema_version=1.0`或正式Pipeline。
- Prompt首次预检：原始FVA Prompt估算`7,405 Token`，超过6,000硬门，预检在模型调用前停止。随后只消除候选IR/Evidence重复列表并使用有字段图例的紧凑表示；所有Check、IR语义字段和Evidence原文仍各完整保留一次。调整后FVA Prompt估算`4,665 Token`。
- 无模型门禁：隔离Framework容器定向测试`59 passed`；包含阶段6.2全部Direct回归、固定Fixture、四领域适配、FVA外部事实保护、未知/重复/缺失Check、错误Evidence、7 Batch峰值并发7、任一Batch失败无部分Bundle及LlmRuntime兼容。
- 固定Fixture和Plan保持不变：93个Block、12个Window、101项IR；Plan ID=`risk-plan-a7b1227bd27984e4cf63ca2e433bf452`；Plan Hash=`sha256:a7b1227bd27984e4cf63ca2e433bf452fef822601fb2f88dd6eb3891565b3eac`。
- 第一次启动真实验收时使用默认租户，模型配置在租户`0`下，调用在模型请求前以`deepseek-v4-pro not found`停止；该次没有模型调用、没有费用，不计入质量验收。
- 正式FVA第1次：Batch=`risk-batch-4e6a742dc3e68bd52ae937ff59048292`；`temperature=0`、`thinking=false`、Tool=0；首轮响应触发可修复门禁后执行唯一一次局部修复。
- 硬失败：修复响应改变了FVA-002的Finding或Evidence，被确定性语义守恒门拒绝；错误码=`RISK_REPAIR_SEMANTICS_CHANGED`；错误=`Repair added, removed, reassigned, or changed Findings/Evidence for FVA-002`；模型调用2次、修复1次、Tool=0。
- 安全门表现：修复层没有以“修好业务判断”为由接受被改变的风险语义，符合阶段6.2冻结的修复边界；但首轮未稳定满足FVA外部事实边界，因此新增领域质量门未通过。
- 原始诊断限制：第一版阶段6.3验收脚本没有在顶层捕获`DirectReviewError`，进程退出后内存中的两次Attempt原文丢失；没有从日志猜测或重构模型原文。该可观测性缺陷需要下一轮授权时先修复。
- 失败Artifact：服务器`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-domain-first-failure.json`；1,968字节；SHA-256=`e9219810c6f89abce01f9350213e6fa16e9af3a56b7ce8a3b2ec2d7bd778b7c8`。
- 停止位置：FVA只执行第1次，剩余2次未执行；PO、ICD、LRE均未执行；Commercial未再次调用；五领域Bundle未执行；Legacy未重跑；Framework/Contract/OpenAPI全量回归未执行。
- Git处理：按授权失败门，不创建提交、不推送；业务实现、测试脚本和复现信息保留在当前工作树，等待用户决定是否继续修订阶段6.3。

结论：阶段6.3执行框架和并发路线的无模型门禁通过，但FVA-002首轮质量稳定性未通过，当前不能声明阶段6.3完成。

### 阶段6.3 FVA专项修订开始前核对单

- 当前阶段：阶段6.3 FVA专项修订；阶段6.3继续未通过。
- 本轮授权文件：`D:\codex\.codex\attachments\a491b638-e208-403d-8f6f-d8d7043be5ee\pasted-text.txt`；11,770字节；497行；SHA-256=`b51275315d7fb22be9b45a83a0f3dfa33eb3503c8854eb2238793a8419e40c72`；最后修改时间=`2026-07-23T13:28:30.9442485+08:00`；已完整读取。
- 当前工作目录/工作树：`D:\contract-risk-review-v1`。
- 当前分支：`feat/contract-risk-review-playbook-v1`。
- 当前HEAD：`0dfc336b98850783e0ae5783af25e805799eeba7`。
- 远程状态：远程同名分支当前为`ea68a7f08f29d143d5e7d7f76eb0852d501e6014`；本地相对共同基线`b471c3624956899706c9aca7d379340cad17e6af`领先5个提交、远程领先1个提交。本轮禁止提交和推送，不拉取、不合并、不覆盖另一工作树；最终报告必须列明该分叉。
- 工作树状态：保留阶段6.3第一轮未提交改动；修改`docs/contract_risk_review_stage_log.md`、`services/contract/capabilities/risk_review.py`，新增`risk_review_bundle.py`、阶段6.3隔离验收脚本和基础Bundle测试。
- 权威文件复核：联合冻结稿SHA-256=`0e1de5f8c7e9d97fdfe17b202c92f084e07ac0c7f17702c9942337504129bbe0`；阶段6设计稿SHA-256=`b0589544ad77df61877983914c682306d9a5ec7c4ee1c82dea4789556a5850a8`；Window阶段日志SHA-256=`ddabfbb6c723e3eae94608cfdd312f5f8a22eeed017fb57cc9f73b3843e47d2e`；已重新核对风险审查、Evidence、Direct、阶段5/5.1和101项IR基线。
- 当前阶段允许修改：失败Attempt的隔离诊断留存；FVA-002三态内部模型、Prompt、确定性一致性校验和语义守恒；FVA固定Fixture期望；本轮所需无模型测试、隔离验收脚本和本阶段记录。
- 当前阶段禁止修改：Java、公开OpenAPI、正式Finding/Evidence DTO、`schema_version=1.0`、Window IR、阶段5.1、商务财务既有行为、PO/ICD/LRE领域规则和Prompt、基础Bundle真实验收、正式环境/容器、阶段6.4及以后代码。
- 必须复用：阶段6.1 Registry/Plan/Context；阶段6.2 Strict Raw/Final、Python确定性reason code、单Unit最多一次结构修复、Evidence Anchor映射、Tool=0、`temperature=0`、`thinking=false`；阶段5.5固定101项IR Fixture。
- 固定Fixture：服务器`/home/aituge/workspace/contract-review-dev/test-artifacts/risk-review-input`；只读使用，不重新解析合同、不生成IR。
- 失败Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-domain-first-failure.json`；预期SHA-256=`e9219810c6f89abce01f9350213e6fa16e9af3a56b7ce8a3b2ec2d7bd778b7c8`；开始编码前重新核对。
- 本阶段模型调用：诊断能力完成后允许一次受控FVA重放以捕获首轮与修复原文；完成Prompt/Schema修订和无模型门禁后，只允许FVA正式验收3次。
- 配置与容器：不得修改配置；允许复用既有隔离服务器容器，在`/tmp`测试副本运行；不得操作正式环境。
- 测试门禁：FVA-002三态正反例、字段组合、外部事实边界、结构修复语义守恒、失败Attempt双份留存、FVA-001～005覆盖、FVA-005唯一`OTHER`、Commercial全回归、基础Bundle无模型34项骨架及`git diff --check`。
- 质量门禁：FVA三次完整覆盖FVA-001～005；FVA-002与人工冻结预期3/3一致；Evidence有效率100%；无依据Finding=0；Tool=0；正常每Batch一次调用；不得把缺少外部材料写成现实无授权。
- 性能门禁：无修复不超过30秒；一次合法局部修复不超过50秒；任一次硬上限60秒。
- 提交信息：无；本轮通过或失败均不提交、不推送。
- 通过后允许进入的下一阶段：无；通过后停止并等待用户决定。
- 失败后必须停止的位置：任一无模型或真实FVA质量/性能门失败即停止；保存完整隔离Artifact，不运行其他领域和Bundle。

### 阶段6.3 FVA专项修订与验收

状态：FVA专项门通过；阶段6.3整体继续未通过。已停止在FVA，不运行PO、ICD、LRE、Commercial真实模型验收或基础Bundle真实验收，不进入阶段6.4；本轮不提交、不推送。

#### 原失败完整复现与根因

- 先实现显式、默认关闭的隔离`AttemptArtifactSink`；只有验收脚本传入Sink时才把完整原始响应写入指定测试Artifact。生产默认不保存完整Prompt或模型原文，普通日志不写合同原文、Token或正式回调。
- 每个Initial/Repair Attempt分别记录：Unit、Batch、业务Attempt、Attempt类型、完整原始响应、解析对象、确定性规范化、Raw/Final/覆盖/Evidence/领域/语义错误、开始结束时间、TTFT、Token、Trace、Provider Request ID、修复原因、前后Finding/Evidence摘要、语义Diff及接受/拒绝原因。
- 新增原子写入：每次模型Attempt完成后立即通过临时文件替换保存；`RISK_REPAIR_SEMANTICS_CHANGED`场景已由无模型测试证明首轮和修复轮都会保留。
- 使用旧Prompt受控重放1次，原失败精确复现：Initial=`17,338ms`、TTFT=`1,437ms`、Prompt=`4,265`、Completion=`1,240`；Repair=`12,286ms`、TTFT=`1,221ms`、Prompt=`7,601`、Cached=`5,504`、Completion=`1,324`；总计2次模型调用、1次修复、Tool=0。
- Initial的Raw业务对象完整覆盖FVA-001～FVA-005，但7条Evidence缺少必填`evidence_type`，Raw Pydantic失败。FVA-002同时把乙方上岗人员资质、劳动合同和社保材料未附，推断为“乙方无资质或授权不足”的实质Finding，混淆了履约人员资质与签约代表权，且把缺少外部材料升级为现实风险。
- Repair只为原7条Evidence补上`evidence_type=TEXT_QUOTE`，没有删除FVA-002 Finding；旧守恒算法把补齐必填结构字段也视为Evidence变化，因此以`RISK_REPAIR_SEMANTICS_CHANGED`拒绝。安全门没有接收改变后的非法结果，但旧算法无法区分“兼容的结构补全”和“业务Evidence变化”。
- 旧Prompt重放结果：`stage63-fva-prechange-replay.json`，SHA-256=`bb535159d96bdff0ab99f51c6338b4a35adb50ee2791625ec5eba40f6bcf71af`；完整Attempt Artifact=`stage63-fva-prechange-attempts.json`，SHA-256=`9577fbdbf0dd81df0963b1c6a58ab9f1dfd4697936eec67903de5fdfc80f0193`。均只保存在服务器隔离测试目录。

#### FVA-002三态和修复语义守恒

- `TEXTUAL_AUTHORITY_RISK`：只接受合同文本明确出现主体/签署人矛盾、无权或越权、授权缺失被文本约定为成立/生效条件、签章主体冲突；必须`REVIEWED`、`external_verification_required=false`、至少一条Finding且每条具有当前合同文本Evidence，纯ABSENCE不能成立。
- `EXTERNAL_VERIFICATION_REQUIRED`：合同没有明确文本冲突，但现实授权只能依赖外部材料确认；必须`REVIEWED`、`findings=[]`、`external_verification_required=true`，Python确定性生成`INSUFFICIENT_EVIDENCE`；decision note必须明确法定代表人证明、授权委托书、董事会/股东会批准、营业执照或内部审批等材料。
- `NO_VISIBLE_ISSUE`：合同文本一致且无特别外部核验线索；必须`REVIEWED`、`findings=[]`、`external_verification_required=false`，不得声称已经核验现实授权。
- FVA-002专属字段不得出现在其他Check；三态、Finding、外部核验标志不一致直接失败，Python不替模型切换法律判断。
- 领域边界新增确定性门：员工、上岗人员、劳动合同、社保、履约能力和服务能力不得进入FVA-002判断或核验说明；相关内容应由其他领域处理。
- 固定Fixture人工期望：A069仅表明双方加盖公章后生效，A070及签署页没有签署人/代表人冲突，合同没有明确无授权、越权或授权书作为成立/生效条件，因此冻结为`EXTERNAL_VERIFICATION_REQUIRED`，无Finding。
- 修复语义守恒：Check状态、decision note、FVA三态和外部核验标志、Finding内容/数量/顺序/等级、Evidence数量/引用/顺序必须不变；只允许为已有Evidence补齐与既有引用形状兼容的`evidence_type`。引用型Evidence只能补`TEXT_QUOTE/CONTEXT`，缺失型只能补`ABSENCE`；改变任何既有类型或业务字段仍返回`RISK_REPAIR_SEMANTICS_CHANGED`。
- Prompt明确每条Evidence必须输出`evidence_type`，给出最小合法TEXT_QUOTE/ABSENCE结构，冻结FVA-002四步判定和三态字段关系；仍为Tool=0、`temperature=0`、`thinking=false`、JSON Object和严格Pydantic。

#### 测试和真实验收

- 隔离服务器无模型门禁：`80 passed`；覆盖FVA显式文本冲突、文本明确无授权、仅缺外部材料、三态组合、外部事实禁断、人员资质越界、非FVA字段污染、结构补Evidence类型、assessment/Finding/Evidence语义守恒、失败双Attempt持久化、FVA-001～005覆盖、FVA-005唯一OTHER、阶段6.2 Commercial全回归、7 Batch/34 Check基础Bundle骨架、Fixture/Plan和LlmRuntime。
- 固定FVA Prompt估算=`5,217 Token`，低于6,000硬门；真实请求Prompt=`4,849 Token`。
- 第一组3次机器门曾全部返回三态正确，但人工复核发现第2次decision note把“上岗人员资质”混入外部核验；该组不作为最终验收。Artifact SHA-256=`b65d231976cb4d7fccac96f1b7ad6ba127c4fa9dd56ea2b065bfac85d05ec3c4`，Attempt SHA-256=`b21ec756c1851cf2b42acb204ee060ba2345a5330c482f4c33ad58f130ff5ad5`，仅作为边界缺陷诊断保留。
- 补充人员资质领域门后，最终3次FVA真实验收全部通过：

| 次数 | 总耗时 | TTFT | Prompt/Cached/Completion/Total Token | 模型/修复/Tool | FVA-002 | Finding/Evidence |
|---:|---:|---:|---|---|---|---|
| 1 | 8,513ms | 1,699ms | 4,849/0/504/5,353 | 1/0/0 | EXTERNAL_VERIFICATION_REQUIRED / INSUFFICIENT_EVIDENCE | 0/0 |
| 2 | 7,207ms | 776ms | 4,849/4,736/444/5,293 | 1/0/0 | EXTERNAL_VERIFICATION_REQUIRED / INSUFFICIENT_EVIDENCE | 0/0 |
| 3 | 6,645ms | 744ms | 4,849/4,736/458/5,307 | 1/0/0 | EXTERNAL_VERIFICATION_REQUIRED / INSUFFICIENT_EVIDENCE | 0/0 |

- 最终性能：min=`6,645ms`、median=`7,207ms`、max=`8,513ms`；均无修复并低于30秒正常路径目标。
- 三次均完整覆盖FVA-001～FVA-005，全部Check状态为`REVIEWED`；FVA-002三次均匹配冻结期望，`external_verification_required=true`、Finding=0；没有把缺少外部材料写成现实无权/未授权，没有人员资质越界。
- 三次模型调用均为1，修复=0，Tool=0；所有Attempt均`ACCEPTED`，Raw/Final/覆盖/Evidence/领域/语义错误均为空。正式Evidence数量为0，因此非法Evidence=0、无依据Finding=0。
- 最终结果Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-fva-three-run-v2.json`；SHA-256=`4635625ae0258be91f3f7e038f0a32605024d0eac7eb851c798e3877fb036f4c`。
- 最终完整Attempt Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-fva-three-run-attempts-v2.json`；SHA-256=`d3f84b064fa1cdd54e36372b1fc9a94fec5cb78ada52a2349173724a49960cd8`。

结论：FVA专项的可观测性、三态边界、结构修复守恒、质量和性能门通过。但按照本轮授权，阶段6.3整体继续保持未通过；PO、ICD、LRE及五领域Bundle尚未真实验收。本轮不创建提交、不推送，停止等待用户决定。

### 阶段6.3 PO专项修订与首次验收

状态：PO专项未通过；阶段6.3整体继续未通过。首次真实验收在第1次运行即发生Evidence硬失败，已按门禁停止；未执行剩余2次PO验收，未执行ICD、LRE或基础Bundle真实验收。本轮不提交、不推送。

#### Git远程跟踪引用修复

- 当前工作树：`D:\contract-risk-review-v1`；分支：`feat/contract-risk-review-playbook-v1`；HEAD：`0dfc336b98850783e0ae5783af25e805799eeba7`。
- GitHub真实远程HEAD、`ls-remote`和本地HEAD均为`0dfc336b98850783e0ae5783af25e805799eeba7`；此前本地`origin/feat/contract-risk-review-playbook-v1`显示为`ea68a7f08f29d143d5e7d7f76eb0852d501e6014`，原因是当前克隆的fetch规则只跟踪`proof`，同名功能分支的本地远程跟踪引用没有随GitHub更新。
- 保留原规则`+refs/heads/proof:refs/remotes/origin/proof`，新增精确规则`+refs/heads/feat/contract-risk-review-playbook-v1:refs/remotes/origin/feat/contract-risk-review-playbook-v1`后执行`git fetch origin --prune`。
- 修复后本地远程跟踪引用与`ls-remote`一致；`HEAD...origin/feat/contract-risk-review-playbook-v1`为`0 0`。没有执行pull、merge、rebase、reset、stash、switch、commit或push，既有未提交阶段6.3工作完整保留。
- 本地保护分支`backup/contract-risk-stage63-local-0dfc336`和工作树外备份`E:\MyProjects\Newestcontract\backups\stage63-fva-git-divergence-20260723-140342`继续保留。

#### PO实现与无模型门禁

- PO-001～PO-007继续复用阶段6.1的Plan、Context、IR、Anchor和Block，复用阶段6.2的严格Raw/Final Pydantic、确定性Evidence校验、单Batch最多一次局部修复、Tool=0、`temperature=0`和`thinking=false`。
- 完成PO检查边界、反例、可执行判定步骤、确定性候选、领域安全门和风险唯一键；没有硬编码当前合同的主体、金额、条款编号或具体文本。
- 固定101项IR Fixture生成2个PO Batch：
  - `risk-batch-912122916f35b00119327feb5893c68d`：PO-001、PO-003、PO-004、PO-007；58项IR、64项Source Excerpt；Prompt估算5,262 Token。
  - `risk-batch-4cf42c87a1b58b5b78acdb8a9588cb53`：PO-002、PO-005、PO-006；60项IR、61项Source Excerpt；Prompt估算5,211 Token。
- 两个Batch均低于6,000 Token硬门，且没有任何Batch收到全部101项IR。
- PO所需IR类型调整后，确定性Plan ID为`risk-plan-2bddbd099160dc98ad2fdc51d85a14df`，Plan Hash为`sha256:2bddbd099160dc98ad2fdc51d85a14df56d25bf2a1a54b8da1484020963d5c37`；相同Fixture重复构建保持稳定。
- 隔离服务器无模型门禁：`84 passed, 1 warning`，耗时`14.08s`。覆盖PO确定性候选、缺失验收候选、领域安全门、风险唯一键、固定Fixture两Batch投影、Prompt硬门、FVA、Commercial、Evidence、修复守恒、Bundle骨架、Plan和LlmRuntime。
- 第一次只读测试因容器内`/workspace/.pytest_cache`不可写而在pytest初始化阶段报错；增加临时`--tmpfs /workspace/.pytest_cache`后全部通过，未修改业务代码或正式容器。

#### 首次真实PO验收与停止点

- 隔离测试容器：`contract-ir-window-stage2-framework`；固定Fixture只读使用；没有操作正式环境、正式容器或正式数据。
- 第1次运行中，PO-002/PO-005/PO-006 Batch首轮成功：模型耗时`16,073ms`，TTFT=`2,479ms`，Prompt/Cached/Completion/Total Token=`4,798/0/1,034/5,832`，模型调用1、修复0、Tool=0。
- PO-001/PO-003/PO-004/PO-007 Batch首轮失败：模型耗时`25,369ms`，TTFT=`2,588ms`，Prompt/Cached/Completion/Total Token=`4,844/0/1,664/6,508`；错误为`RISK_EVIDENCE_LINK_INVALID`。
- 唯一非法配对位于PO-003 Finding“甲方配合义务缺失，乙方承担延迟风险”：模型输出`ir_ref=I039`和`evidence_ref=A044`。`I039`实际是“乙方保证向甲方提供的所有材料真实有效”，唯一允许的Evidence为`A025`；`A044`实际对应“甲方提供的设备或支持无法满足项目要求……”，其Anchor不属于`I039`。
- Prompt中的PO-003确定性候选已经明确给出`candidate_ir_refs=[I039]`、`candidate_evidence_refs=[A025]`，因此该失败不是Fixture、候选生成或Evidence Validator误报，而是模型把另一条语义相关原文与`I039`错误拼接。
- 唯一一次局部Repair耗时`13,407ms`，TTFT=`768ms`，Prompt/Cached/Completion/Total Token=`8,611/6,400/1,364/9,975`。Repair仍保留`I039+A044`，再次返回同一`RISK_EVIDENCE_LINK_INVALID`。
- 失败根因已进一步定位：当前通用Repair约束要求保留首轮原`ir_ref/evidence_ref`及顺序；该约束适合结构字段补全，却使已经错误的IR/Evidence关系无法在修复轮纠正。该问题需要下一轮明确授权后调整“结构修复”和“引用关系修复”的边界，本轮失败后没有继续修改业务实现。
- 结果Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po-three-run.json`，SHA-256=`6e291c5bebd081c5ac3b7b4d09b255f518bbf7e974c39e412bf0d0f7ed7e82ab`。
- 完整Initial/Repair Attempt Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po-three-run-attempts.json`，SHA-256=`261cce4f5ba6a9d7ecd8ce5ba12c9b564b53e2934e956947d59bd2bf1e4c8718`。
- 按授权硬停止：PO第2、3次未运行；未运行ICD、LRE、Commercial重放、五领域Bundle、Legacy或阶段6.4；没有额外模型调用。

结论：PO技术路线、两Batch投影、输入预算、并发骨架和确定性安全门已完成无模型验证，但首次真实验收未通过Evidence关联门，且局部Repair无法纠正错误引用。PO专项不能声明通过，阶段6.3整体仍未通过，不满足进入ICD或后续领域验收的门禁。

### 阶段6.3 PO Evidence Source Bundle修订与专项复验

状态：未通过；首次真实运行仍出现非法Evidence Source，已按硬停止规则终止，未执行第2、3次，不提交、不推送、不进入ICD、LRE、Bundle或阶段6.4。

- A044只读复盘：A044正文为“8.2.2 甲方提供的设备或支持无法满足项目要求，经乙方提出，在30个工作日内仍无法改正时。”；真实绑定为`I054`，IR类型为`rights`，语义是乙方解除权；不在PO-003只允许`obligations`的候选范围。A044能辅助说明“甲方支持不足可能触发乙方解除权”，但不能单独证明“甲方配合义务缺失”，更不能与`I039`拼成一个技术来源。`I039`真实Source仍唯一绑定A025。
- 新增严格的`RiskEvidenceSource`：Source ID由`generation_id + ir_item_id + anchor_id + char_start + char_end + evidence_type`规范化后确定性生成；一个Source不可拆分地绑定Generation、IR、Anchor、Block、字符区间、原文、Hash和IR类型。重复构建ID稳定，跨Generation、错误原文Hash及错误绑定均拒绝。
- 新增`RiskAbsenceEvidenceSource`和每Check的`RiskCheckEvidencePolicy`。缺失类风险只能选择Python预建的ABSENCE Source；模型不能编造checked scope或verification method。
- PO模型Finding只输出`evidence_source_ids`；最终`affected_ir_ids`、Anchor、Block、字符区间、quoted text和Hash全部由Python从Source Bundle确定性派生。未知Source、跨Batch Source和当前Check不允许的Source均返回`RISK_EVIDENCE_SOURCE_NOT_ALLOWED`。
- Repair分为`SCHEMA_REPAIR`与`EVIDENCE_SELECTION_REPAIR`。后者只允许调整Source ID，Finding数量、Check、风险类型、等级、标题核心语义、影响和建议均不得变化；旧式IR/Anchor只在唯一合法Source等六项条件同时满足时允许兼容规范化，并记录计数。
- 新Plan ID为`risk-plan-1749e966e2325e8d6555edc526513418`，Plan Hash为`sha256:1749e966e2325e8d6555edc526513418a92c92eeeb2b7d020f1d556be8dd9d97`。两个PO Batch Prompt估算分别为`3,852`和`2,881` Token，Evidence Source数量分别为17和11；均低于6,000 Token硬门，且没有Batch收到全部101项IR。
- 隔离服务器无模型回归：`88 passed, 6 skipped, 1 warning`，耗时`3.07s`；定向PO Source测试此前单独为`36 passed`。`git diff --check`通过，仅有既存Windows换行提示。
- 首次真实运行第一个完成的Batch为PO-002/005/006：首轮模型耗时`16,090ms`，TTFT=`2,253ms`，Prompt/Cached/Completion/Total Token=`2,709/0/885/3,594`，Tool=0、thinking=false、temperature=0。
- 首轮PO-002 Finding错误选择`risk-es-2cfa05ffc3bc0f28030e8b2fb0f23b19`；该Source真实正文为“为满足本项目目的，甲方在履行过程中提出的要求且乙方能够达到的，乙方应予执行”，只在PO-006允许集合，不在PO-002允许集合，因此首轮命中`RISK_EVIDENCE_SOURCE_NOT_ALLOWED`。这说明新的Validator正确拒绝了跨Check Source，但`Repair=0`质量目标没有达到。
- 脚本随后执行了一次受限`EVIDENCE_SELECTION_REPAIR`：耗时`9,244ms`，TTFT=`1,027ms`，Prompt/Cached/Completion/Total Token=`5,101/3,584/880/5,981`；错误Source被替换为同时允许PO-002/PO-006的`risk-es-9ac678593347e2a0757f8434050799c3`，业务语义保持不变。
- Repair结果随后触发`RISK_PO_DOMAIN_LEAKAGE / IP_CONFIDENTIALITY_DATA`。触发文本来自PO-002修改建议中的“对甲方检查权增加……保密义务”，并非新Evidence Source关联错误。该问题超出本轮只修Evidence绑定模型的授权范围，本轮未修改领域边界规则。
- 结果Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po-evidence-source-run-failed.json`，SHA-256=`4bcda8d3c74a18bec26f376e98b1b26f26ea220358e5330a5fdadd7b38c56ad9`。
- 完整Initial/Repair Attempt Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po-evidence-source-attempts-failed.json`，SHA-256=`0aaeca5759f567877b8f5610fcc4b1b8655b35d956e1558be0024e1d509ae7b7`。
- 按授权硬停止：未运行PO第2、3次，未运行ICD、LRE、Bundle、Commercial、Legacy或阶段6.4，没有操作正式环境和正式容器。

结论：Evidence Source Bundle的确定性绑定、派生和拒绝路径已通过自动测试，A044旧错误已从结构上消除；但真实首轮仍发生跨Check Source选择并使用一次Evidence Selection Repair，且修复结果触发既有PO领域门禁，因此PO子门禁未通过，阶段6.3整体继续未通过，不建议继续ICD。

### 阶段6.3 PO Check级Source隔离与领域门修订

状态：未通过；两个指定缺陷已完成实现和无模型验证，但全新第1次真实PO运行在PO-004 Schema Repair语义守恒门失败。已停止第2、3次，不提交、不推送，不进入ICD、LRE、Bundle或阶段6.4。

- 失败事实继续冻结：`risk-es-2cfa05ffc3bc0f28030e8b2fb0f23b19`真实只归属PO-006；上一轮模型把它选给PO-002属于跨`check_code` Source选择。上一轮Evidence Selection Repair成功换成合法Source，随后因suggestion中的“保密义务”被旧领域门误判为`IP_CONFIDENTIALITY_DATA`。
- `RiskEvidenceSource`新增确定性的`allowed_check_codes`；Plan Builder按共享PO候选规则显式生成Source到Check映射，`RiskCheckEvidencePolicy`必须与Source声明完全一致。一个Source允许多个Check时分别出现在各自允许列表，不因曾分配给某个Check而自动扩散。
- PO Prompt由Batch级全局Source池改为Check级结构：每个`assigned_checks[]`对象自带`deterministic_candidates`、`allowed_evidence_sources`和`allowed_absence_sources`。模型仍返回统一`check_results[]`，但每个Finding只能选择当前Check局部列表中的Source。
- 固定Fixture中，PO-002 Prompt可见7个文本Source，PO-006可见3个文本Source；上一轮误选的`risk-es-2cfa05ffc3bc0f28030e8b2fb0f23b19`只出现在PO-006，不出现在PO-002。两个Batch Prompt估算分别为`4,163`和`3,135` Token，均低于6,000硬门。
- 新Plan ID为`risk-plan-3be4091bdf2f5fd6b1beb1b72ed4c4fb`，Plan Hash为`sha256:3be4091bdf2f5fd6b1beb1b72ed4c4fb3bf6af588549fa7104dd71a688852bdb`。
- PO领域门不再把title、issue、impact和suggestion全部拼接后做关键词一票否决。核心风险只按已分配且已验证的Check/risk_type/Evidence Source及title/issue判断；suggestion中的保密、数据删除、保险、担保、通知、审计和书面确认仅作为辅助整改措施，不单独改变领域。真实的“保密条款缺失”等跨领域核心issue继续触发`RISK_PO_DOMAIN_LEAKAGE`。
- PO专项验收模式新增硬开关：Initial出现`EVIDENCE_SELECTION_REPAIR`类错误时直接保存Attempt并失败，不进入Repair；产品默认受限Repair能力仍保留。
- 无模型门禁：PO定向`41 passed`；完整回归`99 passed, 1 warning`，耗时`15.26s`。覆盖Check级Prompt隔离、专用Source不可见、共享Source显式复用、跨Check首轮立即失败、辅助“保密义务”建议允许、真实保密风险拒绝、Commercial/FVA/Bundle骨架/Plan/LlmRuntime回归。`git diff --check`通过。
- 全新真实运行第1次中，PO-002/005/006 Batch首轮直接接受：模型耗时`8,477ms`，TTFT=`3,762ms`，Prompt/Cached/Completion/Total Token=`3,029/0/297/3,326`；三个Check均`REVIEWED`且无Finding，Evidence Selection Repair=0、跨Check Source错误=0、领域门错误=0。
- PO-001/003/004/007 Batch首轮模型耗时`10,820ms`，TTFT=`3,087ms`，Prompt/Cached/Completion/Total Token=`3,961/0/561/4,522`。它产生1条PO-004高风险Finding，所选3个文本Source和1个ABSENCE Source均来自PO-004局部列表，没有跨Check Source或领域门错误；但模型漏写Finding必填的`check_code/category/risk_type`，触发`SCHEMA_REPAIR`。
- Schema Repair耗时`7,054ms`，TTFT=`1,681ms`，Prompt/Cached/Completion/Total Token=`5,584/4,480/603/6,187`。Repair只补入`check_code=PO-004`、`category=RIGHTS_OBLIGATIONS_IMBALANCE`、`risk_type=SERVICE_LEVEL_RISK`，其余标题、issue、影响、建议和Source ID保持一致；现有通用语义守恒仍把这三个必填结构字段的补全判定为Finding实质变化，以`RISK_REPAIR_SEMANTICS_CHANGED`拒绝。
- 本轮两个目标缺陷没有再次出现：首轮跨Check Source错误数为0，领域门误报为0；“保密义务”辅助建议已通过自动测试。真实模型本次没有生成包含该措辞的Finding，因此没有伪造真实模型验证结论。
- 本轮新阻塞属于既有Schema Repair语义守恒边界，不在“只修Source隔离和领域门”授权范围，未继续修改。
- 结果Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po-check-isolation-run-failed.json`，SHA-256=`0e1494b0a3c5ff2c6bdfa3b6193b85bfad65e155696e07337bd0c907702a4e17`。
- 完整Attempt Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po-check-isolation-attempts-failed.json`，SHA-256=`4d7e02e328715b98ffb3a8a1c7a36b442f828fe62d5349ba51225630e8e775b4`。

结论：Check级Source隔离和字段级领域门通过实现及无模型门禁，首次真实运行也未复现这两个问题；但PO-004结构字段补全被既有语义守恒误判，导致第1次真实Unit未完成。因此PO子门禁仍未通过，不建议继续ICD，阶段6.3整体继续未通过。

### 阶段6.3 PO候选逐项裁决改造前诊断

状态：进行中；本节只记录修改代码前对既有三次真实运行的只读诊断。阶段6.3继续未通过，不进入ICD、LRE、Bundle或阶段6.4。

- 诊断Artifact：`stage63-po-field-enrichment-three-run.json`，SHA-256=`2cf917055cd174b6ad230a278c5f842f2bae8fa05561362373302681d0f35499`；完整Attempt Artifact：`stage63-po-field-enrichment-attempts.json`，SHA-256=`5fc14156cb2d49a3d04df37bef8f90a254a92dec16a8e6033d27c6a267ed537d`。
- 三次运行使用同一Plan：`risk-plan-3be4091bdf2f5fd6b1beb1b72ed4c4fb`，Plan Hash=`sha256:3be4091bdf2f5fd6b1beb1b72ed4c4fb3bf6af588549fa7104dd71a688852bdb`。两个Batch、检查分配、IR投影、允许Evidence Source及9个确定性Candidate完全相同，Candidate生成没有发生波动。
- 两个Batch每次真实Prompt Token分别固定为`4,279`和`3,250`，三次全部`finish_reason=stop`；第三次Completion Token分别为`268`和`524`，明显短于前两次，但不是输出Token截断。
- PO-002、PO-004、PO-005、PO-006、PO-007的Candidate均在第三次输入中继续存在，candidate_id、candidate_type及Candidate Evidence Source与前两次一致。
- 第三次并未返回逐Candidate结构。PO-002、PO-004、PO-005、PO-007只返回Check级`REVIEWED + findings=[]`和一段概括性`decision_note`；没有candidate_id级`NO_RISK`、没有counter evidence，也没有逐候选结论。因此不能把它解释为所有Candidate都已经获得合法负面裁决。
- PO-006第三次仍生成Finding并引用同一`CHANGE_CONTROL_REVIEW` Candidate，但模型自由输出的等级由前两次`HIGH`变为`MEDIUM`，证明Finding存在性之外，Severity也仍存在模型波动。
- PO-007前两次核心风险相同，但Supporting Evidence集合不同；该差异不应再作为核心风险波动，后续稳定性键必须使用Candidate、Verdict、Risk Type、确定性Severity及Primary Evidence。
- 原有波动的根因不是Plan、Candidate或Evidence Source生成不稳定，而是现有开放式输出只要求每个`check_code`出现一次，并把`REVIEWED + findings=[]`直接当作`NO_RISK_IDENTIFIED`；模型可以静默跳过该Check内的Candidate。后续必须以“所有分配Candidate均有且仅有一个合法Decision”为Check完成门禁。
- 固定Fixture只读复核还发现：PO-005的两个Source分别描述“乙方为第三方经营同类业务时的服务水准”和“甲方委托第三方维修”，并不直接证明乙方可以转委托、分包或转让。该Candidate仍应存在并接受语义裁决，但测试Oracle不能把前两次开放式Finding机械当作事实金标准；生产候选生成规则与固定Fixture的候选/裁决Oracle必须分开记录。

结论：候选生成稳定，Run 3属于“候选仍在，但模型没有逐项封闭裁决”，不是候选消失或Token截断。候选覆盖硬门可以直接阻止当前静默漏判；Severity和Primary Evidence还需要同时改为Python确定性权威。

### 阶段6.3 PO候选逐项裁决实现与首次真实验收

状态：未通过；候选逐项裁决的确定性实现、自动测试和无模型回归已完成，但第一次真实模型验收即发生硬门禁失败，已停止剩余四次。未提交、未推送，不进入ICD、LRE、Bundle或阶段6.4。

- 旧三次结果诊断：三次使用相同Plan、Batch、Candidate及Evidence Source。Run 3中PO-002、PO-004、PO-005、PO-007并非明确返回`NO_RISK`，而是只返回Check级`REVIEWED + findings=[]`；因此原波动根因是开放式输出允许模型静默跳过Candidate，而不是Candidate生成、Token截断或Plan波动。
- 内部模型：新增`RiskCandidate`、`CandidateDecisionRaw`、`CandidateDecision`和`CheckDecisionResult`。Candidate固定包含唯一ID、Check、类型、强度、事实、触发/缓释条件、Primary/Supporting/Counter Source边界及Severity规则；模型必须对每个Candidate返回且仅返回一个`RISK`、`NO_RISK`或`INSUFFICIENT_EVIDENCE`裁决。
- 覆盖硬门禁：Candidate缺失、未知、重复、跨Check、跨Batch或跨Generation均拒绝；存在Candidate的Check必须在全部Decision合法后才能标记完成。Repair只允许处理JSON/字段等非语义结构错误，不能新增遗漏Decision或改变Verdict、Severity Factor及Primary Evidence。
- 负面裁决与Evidence：`HARD_RULE`和`STRONG_SIGNAL`的`NO_RISK`必须携带对应允许范围内的Counter Evidence；Primary Evidence由Python固定，Supporting和Counter只能从Candidate各自允许集合选择。Finding、`check_code`、`category`、`risk_type`、Primary Evidence和最终Severity均由Python确定性物化。
- Severity：模型只返回结构化事实因子，Python按`severity_rule_id`计算最终等级；PO-006相同因子不再允许出现HIGH/MEDIUM波动。
- 固定Fixture Oracle：生产泛化规则与测试Oracle分离；Oracle覆盖PO-002、PO-004、PO-005、PO-006、PO-007的Candidate ID、类型、强度、Primary Source、允许Counter及Severity规则，不把当前合同主体、条款号或具体结果硬编码进生产生成器。
- Prompt与性能预算：两个PO Batch保持受控并行；System+User Prompt估算分别为`5,312`和`4,182` Token，均低于6,000硬门。Tool=0、`thinking=false`、`temperature=0`保持不变。
- 自动测试：PO及Bundle定向测试`57 passed`；Framework无实时服务测试回归`292 passed, 2 deselected`；Contract Python回归`116 passed, 11 skipped`；`git diff --check`无错误，仅存在既有Windows换行提示。
- 首次真实验收：固定Fixture、相同Plan和两个Batch；第一个完成的Batch为PO-002/005/006。模型调用`16,369ms`，TTFT=`1,048ms`，Prompt/Cached/Completion/Total Token=`3,704/0/1,142/4,846`，`finish_reason=stop`，没有Schema Repair或第二次模型调用。
- 首次硬失败：PO-002 Candidate返回`RISK`时，把四个已经固定为Primary Evidence的Source ID再次填写进`supporting_evidence_source_ids`。这些Source不属于该Candidate的Allowed Supporting集合，Validator按预期返回`RISK_SUPPORTING_EVIDENCE_NOT_ALLOWED`。这不是Candidate遗漏或无依据`NO_RISK`，但仍是不合法的Evidence角色选择，PO子门禁不能通过。
- 立场复核：固定Fixture为`perspective=PARTY_A`，我方是“杭州戎一教育科技有限公司”，相对方是“苏州爱兔格人工智能科技有限公司”。同一首轮原始输出却写了“我方作为乙方”，并把部分影响叙述建立在乙方立场上。由于校验在Evidence角色错误处先失败，该立场问题尚未进入后续物化门；它仍是独立的真实质量缺陷，不得通过后处理猜改。
- 同批其他裁决：PO-005明确返回`NO_RISK`并引用反向Evidence，理由是“甲方委托第三方维修”不等于“乙方转包/分包”，语义方向合理；PO-006返回`RISK`。但整个Batch因PO-002硬失败，均未成为可接受正式结果。
- 真实验收Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po-candidate-decision-five-run.json`，19,933字节，SHA-256=`82ff06b8314aa9d205b7380259f077f5ca1aef77e312ea7a9064dd9cc5f17d71`。
- 完整Attempt Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po-candidate-decision-five-run-attempts.json`，12,106字节，SHA-256=`1c481fad582e18c1f42a8ced1144893d89d45e155bb43e748184e73282250bbb`。
- 停止执行：结果Artifact状态为`FAILED`，成功Run数为0。剩余Batch结果和剩余四次真实运行均未执行；没有继续调用模型，没有进入ICD、LRE、Bundle或阶段6.4，也没有提交或推送。

结论：候选逐项裁决已经从结构上消除了“Candidate静默跳过”，确定性Candidate、覆盖、Severity、Primary Evidence和Finding物化通过自动门禁；但真实首轮仍暴露Evidence角色理解和甲乙方立场两项质量问题。PO子门禁未通过，阶段6.3整体继续未通过，当前不建议进入ICD。

### 阶段6.3 PO最终职责收缩与真实验收

状态：未通过；模型职责已经收缩为Candidate语义裁决，自动测试、最近失败Attempt离线重放及无模型全量回归均通过，但第1次正式真实验收在Unit合并阶段命中重复同根Finding硬门禁。已停止剩余4次，不提交、不推送，不进入ICD、LRE、Bundle或阶段6.4。

- 本轮授权文件：`D:\codex\.codex\attachments\28b2ea9e-ce48-4a49-8393-e5a271f5b904\pasted-text.txt`，14,989字节，SHA-256=`29ac9eba2542a22910bd5810ed36af708c5e449c6e27290d7672f7714697f115`。
- 模型最终职责：只返回`candidate_id`、`verdict`、非权威`decision_summary`、封闭`severity_factors`、Supporting/Counter Source ID及`recommended_control_codes`。模型输出Schema不再包含主体、立场、Primary Evidence、Check/Category/Risk Type/Risk Level或正式Finding四段文案。
- Python最终职责：从Candidate、Registry、Context、Severity Rule、Evidence Source和Template Registry确定性生成Check、Category、Risk Type、Risk Level、Perspective、我方/相对方、Primary/Final Evidence、Title、Issue、Impact、Suggestion及正式Finding/Evidence。
- Evidence角色：Primary继续由Candidate固定；Supporting只能选择Candidate允许集合。模型把Primary重复填入Supporting时，Python先做差集去重，保留Primary并累加`supporting_primary_overlap_count`，不计Schema/Evidence Repair。未知、跨Check、跨Batch、跨Generation或其他不允许Source仍严格失败。Counter门禁保持不变。
- 主体隔离：`decision_summary`不进入正式Finding；出现甲乙方或主体称谓时只累加`decision_summary_perspective_warning_count`。正式文案直接使用Context的`our_party`、`counterparty`和`perspective`渲染，不做自然语言猜测替换。
- 文案与Control Code：为PO Candidate Type增加确定性Title/Issue/Impact模板；Suggestion由封闭Control Code模板渲染。`ADD_CONFIDENTIALITY_GUARD`可作为PO整改措施且不会改变Finding领域；未知或跨Candidate Control Code直接拒绝。
- Severity：模型只返回成立的封闭事实因子，Python继续以`severity_rule_id`计算最终HIGH/MEDIUM/LOW/INFO；模型没有权威Risk Level字段。
- 固定Fixture Oracle：除原Candidate ID、类型、强度、Primary/Counter及Severity Rule外，增加Allowed Supporting、Allowed Control Code、预期Verdict及最低Severity测试Oracle；生产候选生成器没有写入当前合同主体、条款号或结果。
- 定向及离线测试：固定Fixture与最新失败Attempt同时启用时`62 passed, 1 skipped`。离线重放证明上一轮PO-002的4个Primary/Supporting重叠可确定性去重；旧`decision_note`中的“我方作为乙方”只产生审计告警，Final Finding固定为`PARTY_A`、我方“杭州戎一教育科技有限公司”、相对方“苏州爱兔格人工智能科技有限公司”，且Category、Risk Type、Risk Level、Primary Evidence及Control Code建议均由Python生成。
- 无模型全量回归：Framework首次因临时容器未注入现有Redis地址发生1个环境失败；使用隔离网络已有`framework-redis`配置重跑后`298 passed, 8 skipped, 2 deselected`。Contract Python为`116 passed, 11 skipped`。`git diff --check`无错误，仅有既有Windows换行提示。PO Oracle独立校验输出`PO_ORACLE_OK 9`。
- 第一次启动真实验收时，脚本默认租户没有模型配置，返回`LLM model deepseek-v4-pro not found`；数据库只读核对确认模型登记在`tenant_id=0`。该次没有发出模型请求，不计入五次质量验收。随后显式使用现有租户`0`重新开始正式第1次。
- 正式第1次两个Batch均完成一次Initial调用并被各自Raw、Candidate覆盖、Evidence、Final Pydantic和领域门接受，Repair=0、Tool=0。Batch `4cf42c...`耗时`8,504ms`、TTFT=`1,365ms`、Prompt/Completion/Total=`3,878/715/4,593`；Batch `912122...`耗时`17,955ms`、TTFT=`1,865ms`、Prompt/Completion/Total=`4,891/1,648/6,539`。两批均`finish_reason=stop`。
- Primary/Supporting新规则生效：PO-002重复填入的4个Primary Source被确定性去重，没有再次触发`RISK_SUPPORTING_EVIDENCE_NOT_ALLOWED`。模型也没有再输出正式主体文案；正式Finding主体由Python上下文生成。
- 正式硬失败：Unit合并返回`RISK_UNIT_FINDING_DUPLICATED`。只读离线复盘确认PO-001同时存在`SCOPE_EXPANSION`和`DELIVERY_SCHEDULE_REVIEW`两个Candidate，两者均被裁决为RISK；Python将它们映射为相同`check_code=PO-001`、`risk_type=CORE_OBLIGATION_SCOPE_RISK`、`risk_level=MEDIUM`和完全相同的9个Anchor集合，因此形成两个相同Canonical Risk Key。Finding ID不同不改变其同根重复性质。
- 其他真实裁决：PO-005明确`NO_RISK`且Counter合法；PO-002、PO-006、PO-004的两个Candidate、PO-007均返回RISK。PO-002和PO-006本次Severity Factor较少，Python均计算为MEDIUM；PO-004质量标准Candidate为MEDIUM、验收缺失Candidate为HIGH。即使修复PO-001重复，本轮固定Oracle中的部分最低Severity仍可能继续触发门禁，当前没有修改或重跑。
- 结果Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po-python-materialized-five-run.json`，21,173字节，SHA-256=`154b7ec81cd58e03336c12b7e1f8ec9bc792e76ed26c2119aa7479d9ff1ca0d1`。
- 完整Attempt Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po-python-materialized-five-run-attempts.json`，20,699字节，SHA-256=`e2e0cc6cc320de1786262da39d05885177a58b9db72eb3cdda2c08c7f8ffff4f`。
- 停止执行：Artifact状态`FAILED`，成功Run数为0；剩余4次没有运行，没有继续模型调用，没有进入其他Reviewer、Bundle或后续阶段，没有创建提交或推送。

结论：本轮职责收缩成功解决了Evidence角色冗余和正式主体污染，但Candidate到正式Finding的物化仍缺少“同Check、同Risk Type、同Anchor根因”的确定性合并策略；此外最低Severity Oracle与本次模型事实因子存在差异。PO子门禁未通过，不建议继续ICD，阶段6.3整体继续未通过。

### 阶段6.3 PO-003与PO-006五轮差异复盘

状态：进行中；本节是修改代码前对`stage63-po-root-schedule-five-run.json`及其完整Attempt Artifact的只读复盘。阶段6.3继续未通过，不进入ICD、LRE、Bundle或阶段6.4。

- 五轮使用相同Plan、两个Batch和相同Candidate。PO-003的Candidate ID五次均为`risk-candidate-9f0e7abc6fbab1ba58413f75ebed9f8d`；PO-006的Candidate ID五次均为`risk-candidate-b3ee5f4a798cf39d6b13746cc00de138`。两项Candidate、Primary Evidence、允许Supporting/Counter范围均未变化，差异不是Plan、Candidate或Evidence Source漂移。
- PO-003五次唯一Primary Evidence均为`risk-es-0cb844fa0c19b228854acb1d59a040be`，原文是“乙方保证向甲方提供的所有材料真实有效。”；Supporting Evidence五次均为空。第1次模型把该条自身义务扩展解释为“相对方配合缺口会使我方承担延迟或履约后果”，返回`RISK + SCHEDULE_IMPACT`；后四次均把同一Source作为Counter Evidence并返回`NO_RISK`。
- PO-003第1次没有任何Source直接描述甲方配合、资料提供、审批、确认、设备支持、接口、协作义务，也没有Source证明我方履行依赖该配合；更没有Source建立“配合不足→我方承担工期、费用、违约、验收或付款不利后果”的因果连接。第1次仅根据“乙方保证材料真实有效”推导配合缺失及进度后果，违反了Candidate的成立前提，属于无依据扩展。
- 当前Fixture的PO-003 Oracle冻结为`Final Finding = 0`。现有唯一Source可以证明乙方自身承担材料真实性义务，但不能证明必要配合缺失、履行依赖或不合理后果，不能成为PO-003风险依据。
- PO-006五次Candidate的三个Primary Evidence Source完全一致：`risk-es-2cfa05ffc3bc0f28030e8b2fb0f23b19`、`risk-es-9ac678593347e2a0757f8434050799c3`和`risk-es-7cb56e90cf46d1e0a5cf3ac14281c50c`。核心文本包括“甲方在履行过程中提出的要求且乙方能够达到的，乙方应予执行”“乙方须按甲方要求完成项目任务……按照约定时间节点交付”及作业安全要求。五轮均稳定返回PO-006 `RISK`，确定性因素`UNILATERAL_CONTROL`始终成立。
- PO-006模型原始提议依次为：Run 1=`UNILATERAL_CONTROL, MISSING_CORE_MECHANISM`；Run 2=`UNILATERAL_CONTROL, NO_EFFECTIVE_REMEDY`；Run 3=`UNILATERAL_CONTROL, NO_EFFECTIVE_REMEDY, SCHEDULE_IMPACT, FINANCIAL_IMPACT`；Run 4=`UNILATERAL_CONTROL`；Run 5=`UNILATERAL_CONTROL, MISSING_CORE_MECHANISM`。其余四次没有同时选择三个波动Factor，说明当前模型面对全领域通用Factor池时在“可能后果”和“证据支持的事实”之间发生了随机扩展。
- Run 3的`FINANCIAL_IMPACT`没有引用任何费用、价款、扣款、罚款、违约金、赔偿、成本或额外支出Source，更没有“当前变更安排→金钱后果”的直接因果Evidence，属于无依据扩展。
- Run 3的`SCHEDULE_IMPACT`依赖“按照约定时间节点交付”以及Decision Summary中未经Source绑定的“3小时内提供解决方案”。Primary Evidence虽包含时间字面量，但没有证明新增/变更要求导致工期缩短、延期、逾期责任或顺延缺失，缺少当前PO-006风险到进度后果的因果连接；属于有时间词但无风险因果的扩展。
- Run 3的`NO_EFFECTIVE_REMEDY`没有明示排除异议、整改、复核、顺延、解除或协商的Source，也没有合法`RiskAbsenceEvidenceSource`证明在规定范围内缺少救济程序；仅由“未看到书面确认”自由推断没有有效救济，证据不足。
- PO-006确定性Baseline冻结为`MEDIUM`。当前Fixture没有满足升级到HIGH所需的、经过证据门验证的财务、进度或救济因素，因此Oracle为`Final Risk Level = MEDIUM`；模型多报Factor只能被Python拒绝并记录，不能进入最终等级计算。
- 现有五轮脚本在全部五轮模型调用完成后才统一运行`_validate_unit_runs`。因此Run 1的PO-003 Oracle已经失败，脚本仍继续执行Run 2～5，违反真正Fail Fast要求。后续必须在每轮两个Batch合并后立即执行Hard Gate、Fixture Oracle和与前序成功轮次的稳定性比较，失败即保存当前及既有Attempt并返回，后续轮次调用数必须为0。

结论：PO-003波动来自无效Candidate被交给模型开放裁决；PO-006波动来自模型可见全领域Severity Factor池且Python未逐项执行证据门；验收脚本则缺少轮次内即时门禁。后续修改只处理这三处，不改模型最小裁决协议、Canonical Risk Root总体架构或已冻结PO-001规则。

### 阶段6.3 PO-003、PO-006 Severity与Fail Fast修订验收

状态：PO子门禁通过；阶段6.3整体仍未通过，已停止在PO，不进入ICD、LRE、Bundle或阶段6.4。本轮代码、测试和记录继续保留在未提交工作树中，没有提交或推送。

#### PO-003成立边界

- 风险定义冻结为：相对方必要配合义务不足、缺失或不明确，合同履行依赖该配合，并可能使我方承担不合理履行后果。仅描述我方保证资料真实、人员或服务质量、主动提交材料、一般交付义务或合法合规义务，不构成PO-003。
- 新增`PO003CandidatePrecondition`，至少记录`counterparty_cooperation_required`、`performance_depends_on_cooperation`、`adverse_consequence_to_our_party`、`relief_or_adjustment_missing`、支持Source和未满足条件。明显不满足前提的Candidate不交给模型开放裁决，由Python产生`DETERMINISTIC_PRECONDITION / NO_RISK`。
- 文本型Candidate必须直接涉及相对方提供资料、设备、场地、接口、审批、确认、验收、反馈、人员、技术支持或第三方协调，并证明我方履行依赖或不合理后果；形成正式Finding还必须证明延期、费用、违约、验收/付款受控或缺少顺延/免责等后果。
- 缺失型Candidate必须使用合法`RiskAbsenceEvidenceSource`，包含检查范围、应有配合类型、已检查IR类型、配合必要性和不合理后果；不能根据一条我方义务自由推断相对方配合缺失。
- 当前Fixture的唯一PO-003 Source是“乙方保证向甲方提供的所有材料真实有效。”。前置门识别到该文本没有履行依赖，也没有配合不足导致我方承担不合理后果，因此五轮均确定性返回`NO_RISK`，模型不再裁决该Candidate，Final Finding五轮均为0。历史Run 1的误报离线重放被同一前置门阻止。

#### PO-006 Severity权威边界

- 新增PO Severity Factor Registry，覆盖`UNILATERAL_CONTROL`、`NO_EFFECTIVE_REMEDY`、`BROAD_SCOPE`、`FINANCIAL_IMPACT`、`SCHEDULE_IMPACT`、`OPERATIONAL_IMPACT`和`MISSING_CORE_MECHANISM`。每项记录允许的Check、Candidate类型、Evidence类型、必要文本信号、合法Absence类型及冲突/排除信号。
- 模型只能看到当前Candidate的`allowed_severity_factors`；其返回值只记为`proposed_semantic_severity_factors`。Python逐项生成`validated_semantic_severity_factors`和`rejected_severity_factors`，记录提议、接受、拒绝数量及拒绝原因。一般多报Factor只过滤和审计，不触发Repair，也不改变Candidate Verdict。
- `FINANCIAL_IMPACT`必须存在当前风险直接导致费用、价款、扣款、罚款、违约金、赔偿、成本、额外支出或付款减少/拒付的因果Evidence；合同总价、其他付款条款和一般经营推测无效。
- `SCHEDULE_IMPACT`必须同时有期限、工期、延迟、延期、逾期、顺延、按期或工作日/自然日等时间信号，并证明当前风险导致该进度后果；普通“完成、交付、履行、提供”或孤立时间节点无效。
- `NO_EFFECTIVE_REMEDY`只接受明示排除/限制异议、申诉、整改、补救、顺延、拒绝、解除、复核、协商的文本，或Python生成的合法救济缺失Source；“没有看到条款”不能自由推断。
- 历史Run 3多报的`FINANCIAL_IMPACT`和`SCHEDULE_IMPACT`均以`DIRECT_CAUSAL_EVIDENCE_MISSING`拒绝，`NO_EFFECTIVE_REMEDY`以`VALID_ABSENCE_SOURCE_MISSING`拒绝。离线五轮均只接受`UNILATERAL_CONTROL`，PO-006 Baseline和Final Risk Level均稳定为`MEDIUM`。
- 离线重放Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po003-po006-offline-replay.json`；状态`PASSED`，模型调用0，耗时28ms，SHA-256=`e2b2535c8fbd3550fe3962e77a54381e0b8f9c43d4b4c3af2c7d9fde577dccff`。

#### 验收脚本Fail Fast

- 原脚本先执行五轮、最后统一校验；修订后每轮只并行执行当前两个Batch，Unit合并后立即执行Hard Gate、固定Fixture Oracle和与前序成功轮次的稳定性比较，失败立即持久化结果与Attempt并返回，后续轮次不启动。
- 自动测试模拟Run 1失败，实际只执行Run 1，Run 2～5调用数为0。
- 第一组真实五轮在Run 5发现PO-006合法Supporting Evidence数量变化。旧比较器错误地把全部Supporting Evidence集合纳入Canonical Risk一致性，尽管Root、Candidate、Primary Evidence、Risk Level和Finding数量未变仍判失败。该失败Artifact为`stage63-po003-po006-five-run.json`，SHA-256=`46b566d75309258d31edf5f514e5a6611ef9bce5841585054f4437ca25a6d35b`。
- 稳定性门已修正为比较Canonical Root类型、来源Candidate、最终等级和Core Primary Evidence；Candidate Primary Evidence与Control Code继续由`candidate_core_keys`严格比较。合法Supporting Evidence只要求来源和Evidence有效，不要求模型每轮选择数量相同。自动测试覆盖“Supporting变化不应伪造Root变化”。

#### 第二组真实五轮验收

固定Fixture、Plan、两个Batch、模型`deepseek-v4-pro`、`temperature=0`、`thinking=false`。轮次串行、每轮两个Batch并行。

| Run | PO墙钟 | Batch最大耗时 | Prompt | Cached | Completion | TTFT | Finding | Evidence |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| 1 | 17,221ms | 17,217ms | 9,690 | 9,600 | 2,530 | 1,343/1,196ms | 6 | 36 |
| 2 | 16,492ms | 16,485ms | 9,690 | 9,600 | 2,562 | 930/1,440ms | 6 | 30 |
| 3 | 16,205ms | 16,199ms | 9,690 | 9,600 | 2,536 | 1,694/830ms | 6 | 27 |
| 4 | 16,832ms | 16,826ms | 9,690 | 9,600 | 2,524 | 1,504/1,154ms | 6 | 27 |
| 5 | 16,655ms | 16,648ms | 9,690 | 9,600 | 2,500 | 1,366/1,363ms | 6 | 27 |

- PO墙钟`min/median/max=16,205/16,655/17,221ms`，低于30秒目标；每个Batch均低于30秒。
- 每轮模型调用2次，即每Batch正常调用1次；五轮合计10次。五轮`Repair=0`、`Tool=0`。
- 所有Candidate五轮完整裁决；PO-001两个Candidate保留但只形成一个Root/Finding，Risk Level五轮均为`HIGH`。
- PO-003五轮均为`DETERMINISTIC_PRECONDITION / NO_RISK`，Finding五轮均为0。
- PO-006五轮均为`RISK / MEDIUM`；模型每轮均提议7个Factor，Python只接受`UNILATERAL_CONTROL`，其余6个均按证据规则拒绝并留下原因，未经验证Factor进入等级计算为0。
- 每轮Canonical Root均为6个，Final Finding均为6个；风险根因、Root分组、等级、Primary Evidence、主体视角和Control Code五轮一致。Evidence数量变化只来自合法Supporting选择变化，所有正式Finding均有合法Evidence。
- 主体视角固定为`PARTY_A`，正式我方/相对方由Python Context生成；模型摘要不进入正式Finding。
- 第二组结果Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po003-po006-five-run-v2.json`，状态`PASSED`，SHA-256=`e069fca8a7f9ed68d2a7f9208df627f0179b1e13205b5544421af4bfbc816234`。
- 第二组完整Attempt Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage63-po003-po006-five-run-v2-attempts.json`，SHA-256=`8d6675e715d243b70859d94e16bc8e4e4c34733f0758c8b123fd519999836b5d`。

#### 自动与回归门禁

- 固定Fixture及PO专项：`77 passed, 2 skipped`。
- Framework及风险模块最终回归：`85 passed, 2 skipped`；首次未注入Redis密码属于测试容器环境失败，按隔离Compose现有Redis配置重跑后通过。
- Contract Python：`116 passed, 11 skipped`。
- `git diff --check`通过，仅存在既有Windows换行提示。
- 没有修改Java、公开OpenAPI、正式Finding/Evidence DTO、`schema_version=1.0`、Window IR、Commercial、FVA、ICD、LRE、横向检查、Specialist、阶段5.1、正式Pipeline或正式环境。

结论：PO子门禁通过，可以建议下一步在用户另行授权后继续ICD；阶段6.3整体仍未通过。本轮停止，不提交、不推送、不进入ICD、LRE、Bundle或阶段6.4。

### 阶段6.3 ICD专项开始前核对单与实现差异

- 当前阶段：阶段6.3 `ip_confidentiality_data`（ICD-001～ICD-006）专项；PO子门禁已通过，阶段6.3整体仍未通过。
- 本轮授权文件：`D:\codex\.codex\attachments\246a6161-f2ad-478e-8437-163fd05344a3\pasted-text.txt`；已完整读取。
- 联合冻结稿：`E:\MyProjects\Newestcontract\合同审查一期 Java–Python–Framework 联合技术方案 v1.0（冻结稿）.md`；SHA-256=`0e1de5f8c7e9d97fdfe17b202c92f084e07ac0c7f17702c9942337504129bbe0`；已重新读取并确认公开协议、正式Finding/Evidence、Result Sink和`schema_version=1.0`不变。
- 阶段6设计稿：`docs/contract_risk_review_design.md`；开始前29,624字节、348行，SHA-256=`b0589544ad77df61877983914c682306d9a5ec7c4ee1c82dea4789556a5850a8`；已重新完整读取。
- 阶段日志：开始前100,617字节、703行，SHA-256=`0409b085e7460a7129b08a45bbd5df2ee2b671d7bf2c12056a7b1df00beb417a`；已重新读取既有阶段6.0～PO子门禁记录。
- 当前工作目录/工作树：`D:\contract-risk-review-v1`；分支=`feat/contract-risk-review-playbook-v1`；HEAD=`0dfc336b98850783e0ae5783af25e805799eeba7`；本地与GitHub真实远程同名分支一致。
- 当前工作树：保留未提交的阶段6.3 FVA、PO及共享Direct内核修改；已跟踪修改为阶段日志、`risk_review.py`、`models.py`、`plan_builder.py`和`playbooks.py`；未跟踪文件为`risk_review_bundle.py`、阶段6.3脚本、`po_source_policy.py`及基础Bundle测试。
- 安全基线：本地保护分支`backup/contract-risk-stage63-local-0dfc336`继续指向已提交HEAD；工作树外备份目录`E:\MyProjects\Newestcontract\backups`继续保留；本轮不得清理、覆盖、stash、reset或切换这些未提交工作。
- 当前允许修改：ICD Candidate Registry、Evidence/Absence Source Policy、Severity Factor Registry、Control Code Registry、Finding Template Registry、Canonical Root Policy、固定Fixture ICD Oracle、共享候选裁决内核的必要领域泛化、ICD测试、阶段6.3隔离验收脚本和本记录。
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
