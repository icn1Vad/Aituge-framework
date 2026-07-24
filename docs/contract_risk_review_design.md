# 合同 Risk Review Playbook 化改造设计

状态：阶段 6.0 冻结稿

基线提交：`7e4e9760d8bdc04374c26d8692151a0399574e50`

基线阶段：Contract IR Window 阶段 5.5 最终提交

阶段6.0原提交：`ea68a7f08f29d143d5e7d7f76eb0852d501e6014`

阶段6.0 rebase后提交：`fabd5fd006bc0a0dcd3236aabce213d8e144883f`

分支：`feat/contract-risk-review-playbook-v1`

工作树：`D:\contract-risk-review-v1`

协议约束：Java–Python `schema_version=1.0`、Result Sink、正式 Finding/Evidence DTO 保持不变

## 1. 目标和只读边界

本阶段只改造 `extract_contract_ir` 完成后、进入 `verify_evidence` 前的 Risk Review：

```text
Contract Python RiskReviewPlanBuilder（确定性函数，不是Framework Stage）
→ 隐藏内部Risk Plan接口
→ Framework execute_risk_review_bundle（唯一隐藏模型执行单元）
→ 五个基础Review Unit + 两个条件式横向检查 + 最多两个Specialist
→ RiskReviewBundle
→ 五个旧Stage ID的确定性Adapter
→ 现有阶段5.1语义合并
→ 现有verify_evidence和finalize_review
```

以下能力是只读基础：`parse_contract`、`resolve_parties`、Contract IR Window、300 Token Window、并发10、LangExtract、Unicode对齐、Anchor Mapper、Coverage、局部重试、主体上下文、DATE/AMOUNT规范化、DEFINITION唯一溯源、阶段5.1、Evidence验证和Result Hash。

不得修改Java、前端、数据库、固定OpenAPI、Python/Java状态、Attempt恢复、Result Sink、正式Finding/Evidence DTO、Window实现或`schema_version=1.0`。若基础能力阻塞，记录位置、复现、影响和归属后停止，等待另开修复分支。

### 1.1 阶段6固定测试输入

阶段6统一使用服务器隔离目录`/home/aituge/workspace/contract-review-dev/test-artifacts/risk-review-input`，仅在服务器只读加载，不下载、不加入Git、不重新解析合同、不重新执行主体解析或Window IR抽取。正式环境未显式配置`CONTRACT_RISK_FIXTURE_DIR`时禁止加载Fixture，也不得回退到固定路径。

固定文件及SHA-256：

| 文件 | SHA-256 | 用途 |
|---|---|---|
| `contract-ir-stage-result-service-outsourcing-0829-v1.json` | `bfc3388b471c32938dd8f7092d9c404e4817fa4ade3aff4c951042b9d25ab16c` | 严格`CONTRACT_IR_STAGE_V1`输入 |
| `contract-risk-review-fixture-service-outsourcing-0829-v1.json` | `9a89bde1e4b05e403a6fefe24a50f494feea5af40e7b1d40f4da5fe9b42dc15e` | Window、Block、Offset、Anchor、Coverage和IR |
| `risk-review-context-service-outsourcing-0829-v1.json` | `a76b7059ca41aa4bc3e9c1e0b791dd505138872eb73abc5275d847d6fdecb629` | 固定主体、立场和态度 |

`semantic_ir`是14个分类数组，总计101项，不是101元素的平面数组；固定Fixture包含12个Window，Coverage为93/93 Block、12/12 Section、12/12 Window。测试上下文固定为：我方`杭州戎一教育科技有限公司`，相对方`苏州爱兔格人工智能科技有限公司`，`PARTY_A`，`NEUTRAL`。

阶段6.1使用严格Pydantic Stage模型加载标准Artifact。Fixture Loader只能位于测试工具或隐藏隔离测试适配，不进入公开OpenAPI，不完整打印Fixture内容，不成为生产运行依赖。

## 2. 基线和目标指标

阶段5真实Run显示：旧五个ReAct Risk Stage分两批执行，墙钟约`222.33s`；单Stage耗时`104.56～117.30s`；每Stage调用模型3～4次、Tool 3～4次；五Stage累计Prompt Token超过13万。

目标：

```text
RiskReviewPlan构建P95       <= 1s
单基础Reviewer P95         <= 30s
风险并发层墙钟P95          <= 35s
正常Reviewer Tool调用      = 0
正常每Reviewer模型调用     = 1
常规合同完整任务P95        <= 60s
```

不能通过减少检查清单、降低证据要求、固定截断Finding数量或静默忽略失败实现性能目标。

## 3. 职责和执行链路

### 3.1 Contract Python

`RiskReviewPlanBuilder`是确定性服务或函数，不注册为Framework Stage。它一次读取当前active Parse Generation的完整Contract IR，校验任务身份和Perspective，选择Playbook，构造Unit，投影相关IR与Evidence Source，生成横向候选，计算Plan Hash和Context Hash，并通过`include_in_schema=False`的隐藏严格类型接口返回Plan。

### 3.2 Framework

只新增隐藏模型执行单元`execute_risk_review_bundle`。它读取一次Plan，使用统一`LlmRuntime`执行Direct Structured Review，显式`temperature=0`、关闭Thinking、Tool=0、严格Pydantic解析；Schema失败只修复当前Unit一次；受控并发后生成严格`RiskReviewBundle`。

隐藏Stage成功不进入Result Sink；失败发送冻结`RUN_FAILED`且`stage_id=null`，在`error.details.internal_stage_id`记录内部Stage。对外技术状态映射为现有`RISK_REVIEW`。

### 3.3 五个旧Stage

保留`rights_obligations_review`、`commercial_terms_review`、`liability_termination_review`、`missing_ambiguous_clauses`、`relation_extraction`及原结果类型。改造后它们只能读取Bundle、根据冻结映射投影Finding并输出原Artifact；禁止模型、Tool、ReAct、读取IR/正文或生成新Finding，应为毫秒级步骤。

## 4. 固定Review Unit

| unit_id | 目的 |
|---|---|
| `formation_validity_authority` | 主体、权限、签署、生效和强制性合规 |
| `commercial_financial` | 价款、支付、结算、交付和验收 |
| `performance_obligations` | 履行、配合、变更、保证和权利义务平衡 |
| `ip_confidentiality_data` | 知识产权、保密、数据使用和返还 |
| `liability_remedies_exit` | 违约、赔偿、责任限制、终止和争议解决 |
| `cross_clause_consistency` | 跨条款冲突、引用和优先级；有候选才调模型 |
| `missing_ambiguity_completeness` | 缺失、歧义和结构不完整；有疑似项才调模型 |

## 5. 数据模型

### 5.1 PlaybookManifest

字段：`playbook_id`、`version`、`name`、`description`、`enabled`、`execution_mode`、`criticality`、`applicability`、`required_ir_types`、`target_domains`、`checks`、`risk_level_rule_ids`、`evidence_policy_id`、`deterministic_validator_ids`、`specialist_reviewer_spec`、`test_case_ids`。

执行模式固定为`DETERMINISTIC / EXTEND_DOMAIN / SPECIALIST_REVIEWER`。Playbook不是Prompt文件，必须同时具备Manifest、检查项、Evidence要求、Validator和测试用例。

### 5.2 CheckSpec

字段：`check_code`、`title`、`description`、`domain`、`criticality`、`applicability`、`required_ir_types`、`review_question`、`allowed_categories`、`allowed_risk_types`、`risk_level_rule_id`、`evidence_requirement`、`deterministic_validator_ids`、`execution_mode`、`priority`、`enabled`。

`check_code`全局唯一且版本内稳定，模型不得返回未分配的Check。

### 5.3 RiskReviewPlan

字段：`plan_version`、`plan_id`、`plan_hash`、`review_id`、`document_id`、`generation_id`、`attempt_no`、`perspective`、`contract_type`、`review_attitude`、`selected_playbook_ids`、`review_units`、`deterministic_checks`、`specialist_reviewer_count`、`max_concurrency`、`required_unit_ids`。

Plan Hash使用UTF-8、`ensure_ascii=false`、键排序、紧凑JSON和SHA-256；时间戳不进入Hash。

### 5.4 ReviewUnitSpec

字段：`unit_id`、`unit_type`、`domain`、`required`、`check_specs`、`required_ir_types`、`ir_projection_fields`、`source_selection_policy`、`evidence_policy`、`deterministic_validator_ids`、`model_id`、`output_schema_version`、`temperature=0`、`thinking_enabled=false`、`max_repairs=1`、`timeout_seconds`、输入软硬Token限制、输出Token限制。

### 5.5 RiskReviewContext

字段：`review_id`、`document_id`、`generation_id`、`attempt_no`、`plan_id`、`unit_id`、`perspective`、`our_party`、`counterparty`、`contract_type`、`review_attitude`、`check_specs`、`definitions`、`projected_ir_items`、`clause_catalog`、`source_excerpts`、`source_anchor_index`、`present_ir_types`、`missing_ir_types`、`coverage_summary`、`horizontal_candidates`、`context_hash`。

Reviewer只接收本Unit相关投影，不读取全文。Source Excerpt必须携带当前Generation的Block、字符区间和逐字原文。超出硬预算返回`RISK_CONTEXT_BUDGET_EXCEEDED`，不得静默截断。

### 5.6 FindingDraft

模型输出：`source_unit_id`、`domain`、`check_code`、`category`、`risk_type`、`risk_level`、`title`、`issue`、`impact_to_our_party`、`suggestion`、`perspective`、`our_party`、`counterparty`、`evidence_drafts`。

模型禁止生成`finding_id`、`evidence_id`、`plan_id`、`stable_hash`和时间字段。技术ID由代码根据Plan、Unit、Check、Category、Risk Type及Source Anchor确定性生成。

### 5.7 ReviewUnitResult

字段：`unit_id`、`domain`、`status`、`check_results`、`findings`、`warnings`、`model_call_count`、`repair_count`、四类Token、`duration_ms`、`trace_ids`。

每个`check_result`包含`check_code`、`status: REVIEWED / NOT_APPLICABLE / FAILED`、`reason_code`和`finding_local_ids`。

Direct Reviewer采用严格的Raw/Final两层模型。模型Raw层负责`check_code`、`status`、`decision_note`、`findings`和`evidence`；为兼容模型残留输出，Raw层允许`reason_code: str | null`，但该值始终不可信、不得进入最终结果，也不因缺失、空值或任意值触发模型修复。除该兼容字段外，Raw层继续`extra=forbid`。

Python在完成Raw Pydantic、Check覆盖、Finding和Evidence语义校验后，确定性生成Final层的非空封闭枚举`reason_code`：

- `REVIEWED`且有Finding：`RISK_IDENTIFIED`；
- `REVIEWED`且无Finding：`NO_RISK_IDENTIFIED`；
- `NOT_APPLICABLE`：`NOT_APPLICABLE`；
- `FAILED`：`CHECK_FAILED`；
- 已产生候选但Evidence不足以完成判断：`INSUFFICIENT_EVIDENCE`。

Final层保持严格Pydantic并拒绝`null`和空字符串。补全不得改变模型产生的Finding数量、风险等级、Evidence、Check状态或`decision_note`。具体法律风险根因继续由`FindingDraft.risk_type`、`FindingDraft.issue`和`decision_note`表达，通用`reason_code`不得无限扩展。每个Unit记录`reason_code_enrichment_count`、`reason_code_rule_version`和`ignored_model_reason_code_count`；该确定性补全不计作模型修复，也不增加模型调用。

### 5.8 RiskReviewBundle

字段：`bundle_version`、任务和Generation身份、`plan_id`、`plan_hash`、`unit_results`、`findings`、`evidence_candidates`、`internal_relationships`、`deterministic_check_results`、`completed_unit_ids`、`metrics`、`bundle_hash`。

成功条件：所有必需Unit恰好出现一次；身份一致；所有Check被覆盖；Finding/Evidence引用完整；Specialist不超过2；任一REQUIRED失败时不生成成功Bundle。

## 6. Criticality和check_code门禁

| criticality | 一期处理 | 正式Finding | 技术失败 |
|---|---|---|---|
| `REQUIRED` | 正式启用检查默认值 | 允许 | 任一FAILED导致Risk Review失败 |
| `OPTIONAL` | 一期默认不启用 | 仅完整成功时允许 | 仅Manifest显式允许降级时可降级，失败结果不进入正式Finding |
| `ADVISORY` | 一期默认不启用 | 不允许，仅诊断 | 记录Warning，不影响正式结果 |

`NOT_APPLICABLE`必须由Applicability或确定性Validator给出稳定原因码，不能由模型自由规避。一期基础Playbook的所有正式检查均为`REQUIRED`。

以下情况Unit失败：分配Check缺失；返回未分配Check；同一Check重复且不能确定性合并；REQUIRED返回FAILED；Finding引用不存在的Check；Category/Risk Type不在CheckSpec允许集合；有Finding但Check状态不是REVIEWED。无Finding也必须显式返回`REVIEWED + findings=[]`。

## 7. 一期逐Finding映射表

映射键固定为`domain + check_code + category`。`risk_type`必须属于CheckSpec允许集合但不选择Artifact。未命中、命中多行、使用未授权`OTHER`时整个Risk Review失败。

| domain | check_code | 检查内容 | category | legacy_artifact_type |
|---|---|---|---|---|
| formation_validity_authority | FVA-001 | 主体名称、身份和对应关系 | PARTY_IDENTIFICATION | rights_obligations_review_result |
| formation_validity_authority | FVA-002 | 主体资格、代表权和授权 | PARTY_IDENTIFICATION | rights_obligations_review_result |
| formation_validity_authority | FVA-003 | 签字、盖章及必要签署形式缺失 | MISSING_CLAUSE | missing_ambiguous_clauses_result |
| formation_validity_authority | FVA-004 | 生效条件、时间和前置条件不清 | AMBIGUITY | missing_ambiguous_clauses_result |
| formation_validity_authority | FVA-005 | 强制性规范、禁止性安排或基础效力风险 | OTHER | rights_obligations_review_result |
| commercial_financial | CF-001 | 价款、计价口径和总额 | PAYMENT | commercial_terms_review_result |
| commercial_financial | CF-002 | 付款节点、条件和期限 | PAYMENT | commercial_terms_review_result |
| commercial_financial | CF-003 | 发票、税费和开票前置条件 | PAYMENT | commercial_terms_review_result |
| commercial_financial | CF-004 | 调价、扣款、抵销和结算调整 | PAYMENT | commercial_terms_review_result |
| commercial_financial | CF-005 | 预付款、保证金和履约保障 | PAYMENT | commercial_terms_review_result |
| commercial_financial | CF-006 | 币种、账户和支付路径 | PAYMENT | commercial_terms_review_result |
| commercial_financial | CF-007 | 交付物、时间和交付方式 | DELIVERY | commercial_terms_review_result |
| commercial_financial | CF-008 | 验收标准、程序、期限和后果 | ACCEPTANCE | commercial_terms_review_result |
| performance_obligations | PO-001 | 双方核心义务范围 | RIGHTS_OBLIGATIONS_IMBALANCE | rights_obligations_review_result |
| performance_obligations | PO-002 | 权利义务和单方控制权平衡 | RIGHTS_OBLIGATIONS_IMBALANCE | rights_obligations_review_result |
| performance_obligations | PO-003 | 配合义务、前置依赖和边界 | RIGHTS_OBLIGATIONS_IMBALANCE | rights_obligations_review_result |
| performance_obligations | PO-004 | 服务标准、响应时限和SLA | RIGHTS_OBLIGATIONS_IMBALANCE | rights_obligations_review_result |
| performance_obligations | PO-005 | 转委托、分包和权利义务转让 | RIGHTS_OBLIGATIONS_IMBALANCE | rights_obligations_review_result |
| performance_obligations | PO-006 | 需求、范围和价格变更控制 | RIGHTS_OBLIGATIONS_IMBALANCE | rights_obligations_review_result |
| performance_obligations | PO-007 | 质保、维护、整改和支持 | RIGHTS_OBLIGATIONS_IMBALANCE | rights_obligations_review_result |
| ip_confidentiality_data | ICD-001 | 项目成果和新增知识产权归属 | INTELLECTUAL_PROPERTY | liability_termination_review_result |
| ip_confidentiality_data | ICD-002 | 背景知识产权、许可范围和限制 | INTELLECTUAL_PROPERTY | liability_termination_review_result |
| ip_confidentiality_data | ICD-003 | 第三方权利和不侵权救济 | INTELLECTUAL_PROPERTY | liability_termination_review_result |
| ip_confidentiality_data | ICD-004 | 保密范围、例外、期限和披露 | CONFIDENTIALITY | liability_termination_review_result |
| ip_confidentiality_data | ICD-005 | 数据使用、处理、安全和访问 | CONFIDENTIALITY | liability_termination_review_result |
| ip_confidentiality_data | ICD-006 | 数据权属、返还、删除和留存 | CONFIDENTIALITY | liability_termination_review_result |
| liability_remedies_exit | LRE-001 | 违约触发和责任成立条件 | BREACH | liability_termination_review_result |
| liability_remedies_exit | LRE-002 | 违约金、损失计算和调整 | BREACH | liability_termination_review_result |
| liability_remedies_exit | LRE-003 | 责任上限、免责和间接损失 | LIABILITY | liability_termination_review_result |
| liability_remedies_exit | LRE-004 | 赔偿、补偿和第三方索赔 | LIABILITY | liability_termination_review_result |
| liability_remedies_exit | LRE-005 | 解除、终止和单方退出 | TERMINATION | liability_termination_review_result |
| liability_remedies_exit | LRE-006 | 终止后的结算、返还和继续义务 | TERMINATION | liability_termination_review_result |
| liability_remedies_exit | LRE-007 | 不可抗力、情势变化和风险分配 | LIABILITY | liability_termination_review_result |
| liability_remedies_exit | LRE-008 | 适用法律、争议方式和管辖 | DISPUTE_RESOLUTION | liability_termination_review_result |
| cross_clause_consistency | CCC-001 | 日期、期限、金额和比例冲突 | INTERNAL_CONFLICT | relation_extraction_result |
| cross_clause_consistency | CCC-002 | 权利、义务和条件前后冲突 | INTERNAL_CONFLICT | relation_extraction_result |
| cross_clause_consistency | CCC-003 | 条款/附件引用和优先级冲突 | INTERNAL_CONFLICT | relation_extraction_result |
| cross_clause_consistency | CCC-004 | 付款、验收、违约、解除联动冲突 | INTERNAL_CONFLICT | relation_extraction_result |
| cross_clause_consistency | CCC-005 | 主体、定义和术语不一致 | INTERNAL_CONFLICT | relation_extraction_result |
| missing_ambiguity_completeness | MAC-001 | Playbook要求的必要条款缺失 | MISSING_CLAUSE | missing_ambiguous_clauses_result |
| missing_ambiguity_completeness | MAC-002 | 关键商务要素或执行机制缺失 | MISSING_CLAUSE | missing_ambiguous_clauses_result |
| missing_ambiguity_completeness | MAC-003 | 未定义术语、多义和指代不清 | AMBIGUITY | missing_ambiguous_clauses_result |
| missing_ambiguity_completeness | MAC-004 | 条件、期限、标准或后果不完整 | AMBIGUITY | missing_ambiguous_clauses_result |
| missing_ambiguity_completeness | MAC-005 | 空白、待定、占位符和断裂引用 | AMBIGUITY | missing_ambiguous_clauses_result |
| missing_ambiguity_completeness | MAC-006 | 必要救济、退出或争议机制缺失 | MISSING_CLAUSE | missing_ambiguous_clauses_result |

`FVA-005 → OTHER`已经确认，是唯一显式允许的`OTHER`，用于正式Category尚无专门枚举的基础效力/强制性规范风险，不是兜底。只有`domain=formation_validity_authority`、`check_code=FVA-005`且`risk_type=MANDATORY_RULE_OR_VALIDITY_RISK`时允许；来源Unit、domain或risk_type不一致，以及其他Check返回`OTHER`时，整个Risk Review输出非法。

## 8. 横向候选生成

### 8.1 cross_clause_consistency

Contract Python构建内部图：Clause、IR Item、Definition、DATE、AMOUNT、Party为节点；显式引用、相同业务槽位、条件依赖和优先级为边。仅为以下情况生成候选：同槽位不同日期/金额/比例；同一事项的互斥权利义务；引用不存在；优先级循环；付款/验收/违约/解除条件无法同时满足；同一术语或主体含义不一致。

无候选时CCC检查直接`REVIEWED`且模型调用0；有候选时模型只接收候选对、对应IR和Source Excerpt，不得阅读全文或创建候选外关系。

### 8.2 missing_ambiguity_completeness

候选来自：Playbook Required Check与IR/Clause覆盖差集；必填业务字段为空；`待定/另行协商/____/XXX`等占位形式；断裂条款/附件/定义引用；时间、金额、标准、主体或后果缺少组成部分；短语存在多个未消解指向。

无候选时MAC检查确定性返回`REVIEWED`或`NOT_APPLICABLE`且模型调用0；有候选时模型只裁决候选，不得重新扫描全文。

### 8.3 commercial_financial稳定判定补充

`CF-003`只审查发票类型、含税口径、税负承担、开票时限及其与付款条件的先后关系。付款早于合法发票、含税/税负不明可能增加我方价款、发票条件冲突或不可控时触发；总价明确含税且对方应在付款前提供约定发票，以及仅缺少具体税率但不增加我方暴露，不构成风险。现有条款风险引用原文，纯缺失风险使用合法ABSENCE。基础价款归CF-001、付款时点归CF-002、调价抵扣归CF-004、预付款保障归CF-005。

`CF-004`只审查调价、考核扣款、抵销、费用扣减和结算调整。对方可单方提高我方付款、调整机制缺少触发条件/公式/后果、条款冲突导致重复付款或无法结算时触发；完整且有利于我方的扣款/抵销权、固定总价无调整、仅缺少一般抵销条款不构成风险。Finding必须引用具体调整或结算原文，不允许仅凭ABSENCE生成。基础价款、期限、发票和预付款保障分别归CF-001/002/003/005。

`CF-005`固定两阶段判断：先确认我方是否在主要履约、交付或验收前支付全部或绝大部分价款，再确认是否存在履约保函、保证金、分期/里程碑、验收挂钩、退款返还、托管、担保或等效保障。提前支付全部或至少70%且无有效保障时必须输出HIGH；30%至不足70%无保障或保障明显不足时输出MEDIUM。风险Evidence必须同时包含付款时点/比例的文本证据和检查不到保障的ABSENCE。存在有效保障时返回`REVIEWED + findings=[]`，并在`decision_note`说明保障类型。CF-002审付款时间，CF-005独立审付款后的履约或返还保障；同一原文可以同时支撑二者。

三个Check均遵循：Finding只表示对我方不利的实质风险；有利、中性或一般说明不得作为LOW/INFO Finding。输出Category均为`PAYMENT`，风险等级按材料性、金额暴露和可执行性固定，不得因措辞风格改变根因或等级。

### 8.4 ip_confidentiality_data确定性边界

`ICD-001～ICD-006`复用“确定性Candidate → 最小模型Decision → Python Evidence/Severity → Canonical Root → 确定性Finding”链路。每个Check拥有独立的文本Source规则、合法Absence规则、Candidate类型、Control Code、Severity Factor证据门和Finding模板；模型不得创建Candidate、Primary Evidence、Category、Risk Type、Risk Level、主体或正式Finding文案。

六项检查分别覆盖：项目成果权属、背景知识产权许可、第三方知识产权保证与救济、保密机制、数据处理与安全、数据返还删除留存。只有合同技术文本存在对应业务场景时才允许生成缺失型Candidate；未出现知识产权、数据处理或返还删除场景时不得凭一般服务关系推断缺失风险。文本Candidate与缺失Candidate默认是不同Canonical Root，不因同一Check自动合并。

`ICD-004`必须先执行立场前置门。若Primary Evidence仅要求相对方保护我方商业秘密，且没有同时对我方施加不利保密负担，则该文本Candidate由Python确定性返回`NO_RISK`；保密定义、例外、允许披露对象、法定披露程序和期限是否完整，继续由独立、合法的Absence Candidate审查。不得让模型把有利保护条款本身反转为我方风险，也不得因此跳过完整性检查。

Severity Factor只允许从当前Check和Candidate的闭集选择。模型返回值是提议，Python按文本或合法Absence Source逐项验证；`MISSING_CORE_MECHANISM`、权属不清、超范围转让、排他/不可撤销、无限范围、终止后影响、单方保护、第三方暴露、无返还删除、安全标准缺失和事件通知缺失均须有对应证据。财务、进度、履行和救济影响继续使用严格因果门。未通过的Factor只过滤并审计，不进入最终等级，也不触发Repair。

扩展Playbook可以向`ip_confidentiality_data`增加新的Check。未登记在一期`ICD-001～006` Source Registry中的扩展Check继续以自身`required_ir_types`投影Source，并使用通用缺失规则；不得因固定ICD Registry查找而失败。扩展Check若要采用更严格的领域词形或完整性规则，应单独冻结并注册。

### 8.5 liability_remedies_exit确定性边界

`LRE-001～LRE-008`继续复用“确定性Candidate → 最小模型Decision → Python Evidence/Severity → Canonical Root → 确定性Finding”链路。八项检查覆盖违约触发和责任成立、违约金及损失计算、责任上限和免责、赔偿与第三方索赔、解除终止、终止后结算返还、不可抗力，以及适用法律和争议管辖。义务本身不平衡归PO；知识产权、保密和数据根因归ICD；LRE只处理违反义务后的责任、赔偿、解除、退出和争议机制。

责任缺失、责任上限缺失、终止结算缺失、不可抗力缺失和争议解决缺失只能由合法`RiskAbsenceEvidenceSource`支撑。责任上限被绕过必须同时存在上限文本和可能绕过上限的责任文本。Primary Evidence由Python固定；Supporting和Counter严格限定在当前Candidate白名单内。模型不得借用同一Canonical Root中其他Candidate的Source；当Supporting白名单为空时必须返回空数组。LRE判`NO_RISK`时允许将当前Candidate的Primary Source作为明确Counter，Python负责去重和角色校验。

Severity Factor为封闭Registry。模型只提议，Python逐项验证文本信号、因果关系和Absence类型；未经验证的Factor过滤并留痕，不进入等级。累计金钱救济本身固定为`MEDIUM`，一般`FINANCIAL_IMPACT`属于该风险的内生后果，不重复升级；无上限责任、间接损失或上限绕过由独立`UNBOUNDED_LIABILITY_EXPOSURE` Root计算`HIGH`。固定Fixture中不可抗力与争议解决Absence Source已确定性证明核心机制和救济路径缺失，因此二者固定包含`MISSING_CORE_MECHANISM`和`NO_EFFECTIVE_REMEDY`，不依赖模型是否重复提议。

只有Registry明确允许、Root Type一致且核心Evidence指向同一法律后果的Candidate才能合并。`BROAD_BREACH_TRIGGER_REVIEW`、开放赔偿、间接损失和责任上限缺失可以共同形成`UNBOUNDED_LIABILITY_EXPOSURE`；累计救济、不可抗力缺失和争议解决缺失继续保持独立Root。Candidate层允许合法Control Code子集变化，但最终Canonical Root的分组、等级、Core Evidence和Control Code集合必须稳定。

## 9. Direct调用、并发和预算

正常调用：Tool=0、模型调用=1、`temperature=0`、`thinking=false`、严格Pydantic。仅JSON、Schema或Check覆盖错误允许修复当前Unit一次；第二次失败则Unit失败。网络、超时和拒绝不得伪装为空Findings。

Plan Builder中的输入估算是`Business Context`确定性相对大小，不是模型服务的
真实Prompt Token。它继续用于IR/Evidence投影、Batch拆分和防止上下文无限增长；
兼容字段`estimated_input_tokens`的准确语义为
`estimated_business_context_tokens`。

| Business Context项目 | 软限制 | 硬限制 | 超限处理 |
|---|---:|---:|---|
| 公共上下文 | 600 tokens | 800 tokens | Plan失败 |
| 单基础Unit上下文（目标2,000～4,000） | 5,000 | 6,000 | 按check_code确定性拆Batch，仍超限则Plan失败 |
| 单横向Unit上下文（目标1,000～3,000） | 4,000 | 5,000 | 按check_code确定性拆Batch，仍超限则Plan失败 |
| 单Specialist上下文（目标2,000～4,000） | 5,000 | 6,000 | 按check_code确定性拆Batch，仍超限则Plan失败 |
| 单Unit输出（目标不超过1,500） | 2,500 | 4,000 | Unit失败，不截断 |

Provider Prompt预算政策版本固定为`2.0`，权威口径只能是模型服务返回的
`usage.prompt_tokens`：

| Provider Prompt Token | 预算状态 | 运行处理 |
|---:|---|---|
| `<= 6,000` | `WITHIN_TARGET` | 正常通过 |
| `6,001～7,000` | `SOFT_WARNING` | 记录结构化告警，Batch、Unit和Bundle仍可成功 |
| `> 7,000` | `HARD_LIMIT_EXCEEDED` | `RISK_PROMPT_TOKEN_HARD_LIMIT_EXCEEDED`，按既有原子规则使Batch、Unit和Bundle失败 |
| Provider Usage缺失 | `PROVIDER_USAGE_UNAVAILABLE` | 保留诊断，不使用本地估算伪造Provider Token |

`cached_tokens`是`prompt_tokens`的子集，不得重复相加。本地
`Qwen3-32B-Tokenizer`对System Prompt与User Prompt的估算仅记录为
`client_estimated_prompt_tokens/client_tokenizer_name`，用于调用前诊断、相对变化
监控和异常膨胀预警；由于本地Tokenizer与Provider Tokenizer不同，不得据此触发
Provider硬失败。

性能门禁：`commercial_financial`无修复目标不超过30秒；发生一次合法局部Schema修复允许不超过50秒；单Unit硬上限60秒。五个基础Reviewer并行后，Risk Review正常路径目标不超过35秒，偶发局部修复允许不超过60秒。完整合同审查正常路径尽量不超过60秒，偶发局部修复允许不超过90秒。

优化只能移除重复技术字段、按IR类型投影、只带候选Excerpt；不得删除Check。预计超过硬上限时，Plan Builder按`check_code`确定性拆Batch，不允许静默截断。模型不得复述合同、输出分析过程、重复输出相同Evidence，也不得生成Python能确定性补全的ID、页码、Hash和技术字段。

常规合同正常调用5～7次；最多两个Specialist后正常硬上限9次；Schema修复只重试失败Unit，不得因一个Unit失败重跑全部Unit；每Unit最多一次修复，18次仅是异常理论硬上限。同一时刻最多7个模型调用，Specialist等待槽位。七个固定Unit支持同时启动；无候选横向Unit立即完成。并发由隐藏执行单元内部Semaphore控制，不修改Framework全局并发3。

## 9.1 五领域基础 Bundle 执行边界

阶段 6.3 的基础风险审查固定为 5 个 Review Unit、7 个模型 Batch 和 34 个
Check。`formation_validity_authority`、`commercial_financial`、
`ip_confidentiality_data`各 1 个 Batch，`performance_obligations`和
`liability_remedies_exit`各 2 个 Batch。7 个 Batch 之间没有业务依赖，必须
一次性受控并发启动，不得形成分批屏障；Bundle 层不增加模型总结、合并或复核
调用。

Bundle 只负责调度、权威身份一致性、Unit 结果收集、指标汇总、原子失败和最终
封装，不重新解释 Candidate Verdict、Canonical Root、Risk Level、Primary
Evidence 或 Finding。所有 Batch 必须共享同一 review、document、generation、
contract hash、schema、视角、双方主体、审查态度、Fixture、Plan ID 和 Plan
Hash。任一 Batch、Unit、Check、Evidence 或身份门禁失败时，Bundle 返回严格
`FAILED`诊断对象，不返回可消费的部分 Findings；已完成 Batch 只进入诊断
Artifact。

跨 Unit 只做技术身份和同 Check 重复硬门禁。不同领域共享 Evidence 只记录为
`cross_unit_overlap_candidates`，不自动合并或删除，阶段 5.1 语义合并仍由后续
授权单独接入。

稳定性比较继续执行各领域已经冻结的 Oracle。Commercial 按阶段 6.2 的既有
边界处理：CF-003、CF-004 状态以及 CF-005 风险根因、等级、付款文本 Source 和
履约保障 ABSENCE 必须稳定；CF-007、CF-008 等非核心附加 Finding 可以合法
变化，必须披露但不能仅因数量变化判 Bundle 失败。PO、ICD、LRE 继续严格比较
Candidate、Canonical Root、等级和 Primary/Core Evidence。

Bundle 指标固定记录排队耗时、Batch 启动偏移、Batch/Unit/Bundle 墙钟、TTFT、
四类 Token、Repair、Tool、最慢 Batch、最慢 Unit 和峰值并发。固定 Fixture
验收要求`peak_concurrency=7`、每轮 7 次正常模型调用、`Repair=0`、`Tool=0`，
Bundle 墙钟目标不超过 30 秒、允许不超过 45 秒、硬上限 60 秒。

Bundle同时记录`prompt_budget_policy_version`、
`prompt_budget_warning_count`、`prompt_budget_hard_failure_count`、
`max_provider_prompt_tokens`、`batches_over_target`和
`batches_over_hard_limit`。软告警不改变结果原子性；任一Provider Prompt硬超限
仍按必需Batch失败处理，不返回部分正式Finding。

## 10. complete_with_usage观测协议

保持`LlmRuntime.complete() -> str`兼容，新增`complete_with_usage() -> LlmCompletionResult`，至少返回：`content`、`prompt_tokens`、`cached_tokens`、`completion_tokens`、`total_tokens`、`time_to_first_token_ms`、`model_duration_ms`、`trace_id`、`provider_request_id`、`finish_reason`。

为获得真实TTFT，新方法内部使用流式响应并在Framework拼成完整Content，对Unit仍是一次Direct调用。模型端点必须返回最终Usage；Usage缺失返回`MODEL_USAGE_MISSING`，不得写0。每次调用绑定`review_id`、`framework_run_id`、`review_unit_id`、`attempt_no`和`repair_no`。

## 11. Adapter和现有下游

Bundle Builder确定性生成稳定Finding/Evidence ID。五个Adapter按第7节逐条映射并组装现有Stage Result。执行前验证：映射唯一；Category与Check一致；Evidence完整；Perspective/主体一致；每个Finding只进入一个Artifact；五Artifact并集等于Bundle全集。

之后原样复用阶段5.1、Contract Python `_merge_review_artifacts`、Evidence Validator和Finalizer。公开`relationships`仍为空。

## 12. Playbook样例和回滚

首个样例为`software_ip`，阶段6.5只实现`DETERMINISTIC + EXTEND_DOMAIN`，不启用Specialist。新增Playbook不得修改DAG、公开Stage、五Adapter或正式DTO。

计划开关：

```text
CONTRACT_RISK_REVIEW_ENGINE=legacy|shadow|direct
CONTRACT_RISK_REVIEW_MAX_CONCURRENCY=7
CONTRACT_RISK_REVIEW_MODEL_ID=<existing model id>
CONTRACT_RISK_REVIEW_PLAYBOOKS=base_neutral,software_ip
```

6.1～6.7默认`legacy`；Shadow不覆盖正式Artifact；6.8仅隔离测试环境切`direct`。回滚恢复`legacy`并重启测试容器，不涉及数据库。

## 13. Shadow Compare质量集

质量集包含：服务器隔离环境中的既有授权样本（Git只记录Hash和Artifact引用）；至少12份无敏感Fixture，覆盖服务、采购、软件许可、数据处理、保密、咨询、交付验收、责任限制、单方解除、争议管辖、主体授权和内部矛盾；原始规范版与人工风险版按PARTY_A/PARTY_B分别审查。

每份Fixture冻结适用Check、预期状态、关键风险、允许Category、Evidence Span和不应出现的误报。

门禁：REQUIRED覆盖100%；Golden关键风险召回100%；Evidence验证100%；Schema合法100%；无候选外横向Finding；5.1后重复不高于Legacy；甲乙方视角正确反转；Direct不得减少检查项。

## 14. 可观测指标

Plan记录`plan_build_ms/context_build_ms/plan_hash/selected_playbooks/unit_count/specialist_count`及`estimated_business_context_tokens`。每Unit记录排队、TTFT、模型、解析、修复、总耗时，模型/修复/Tool次数，四类Token，Provider Prompt预算结果，Check状态计数，Finding/Evidence数量及Trace，以及`reason_code_enrichment_count/reason_code_rule_version/ignored_model_reason_code_count`。Bundle记录墙钟、峰值并发、总调用、失败Unit、Prompt预算软告警/硬失败、最大Provider Prompt、5.1前后重复数和Evidence通过率。

## 15. 阶段6.1～6.9门禁

| 阶段 | 修改范围 | 测试门禁 | 质量/性能门禁 |
|---|---|---|---|
| 6.1 | 模型、基础Manifest、Router、Plan Hash、隐藏接口 | 确定性、Applicability、Specialist上限、预算、公开OpenAPI不变 | Plan P95<=1s，相同输入相同Hash |
| 6.2 | `commercial_financial` Direct A/B、Usage | Tool=0、正常1调用、一次修复、Token归属 | 无修复<=30s、一次修复<=50s、硬上限60s，Evidence 100%，关键商务召回稳定 |
| 6.3 | 五基础Unit、Bundle校验 | 五路并发、Check覆盖、REQUIRED失败、预算超限 | 无固定4条截断，质量不低于Legacy |
| 6.4 | 两横向候选和条件裁决 | 无候选0调用、有候选1调用、候选外输出拒绝 | 不读全文、不伪造关系、不重复基础风险 |
| 6.5 | `software_ip`样例 | 适用/不适用、注入、调用数不增 | 不改DAG、Adapter、DTO |
| 6.6 | Bundle映射和现有5.1 | 重放19→15、SKIPPED、跨Category不合并、全集守恒 | 5.1安全边界不变 |
| 6.7 | Legacy/Direct Shadow | 质量集、耗时、Token、Evidence、Check覆盖 | 关键风险零漏报、Evidence 100%、Risk P95<=35s |
| 6.8 | 仅测试环境切Direct | Java→Python→Framework、状态、取消、Attempt、Sink、Hash | OpenAPI一致、完整任务P95<=60s、可回滚 |
| 6.9 | 正式切换后单独清理Legacy | 全量回归和镜像验证 | 多类型验收、性能稳定、用户单独确认 |

任何阶段门禁失败立即停止，不进入下一阶段。每阶段独立测试、中文提交、推送和验收。

## 18. 阶段6.4横向审查冻结实现

阶段6.4在五个基础Review Unit之后执行两个横向Unit：

```text
cross_clause_consistency
missing_ambiguity_completeness
```

执行顺序固定为基础7 Batch并发完成并确定性合并后，Python构建关系索引、横向Candidate和Finding所有权，再并发执行横向Batch。两个横向Unit覆盖11项Check，与基础34项合计45项。横向层只处理跨条款冲突、定义与引用关系、程序链和必要机制缺失，不重新解释或修改基础Candidate、Root、等级和Primary Evidence。

横向Finding所有权由Python按`BASE_DOMAIN`、`HORIZONTAL`和`SHARED_CONTEXT_ONLY`确定。基础领域已完整表达的风险只建立链接，不重复物化Finding；阶段5.1模型语义合并不在本阶段接入。所有横向Evidence和Absence Source必须属于当前Generation；跨Generation Source在Extended Bundle封装前以`EXTENDED_HORIZONTAL_EVIDENCE_STALE`失败。

FVA五项Check虽然共用一个模型Batch，但输入按Check独立投影。FVA-002只允许主体、签署、代表权、授权和生效审批材料；人员资质、劳动用工、社保、团队配置、技术能力、项目经验和一般履约能力在Candidate生成、Evidence Source Policy、Check级Prompt和最终领域门四层隔离。每个Batch输入均记录上下文、Check、Candidate、Evidence Policy和Prompt哈希，不保存完整Prompt到普通日志。相同Plan并发构建必须保持哈希稳定，横向Candidate构建不得修改基础Plan。

Extended Bundle实行结果原子性：任一基础Batch/Unit、横向Candidate构建、横向Batch/Unit、所有权、Evidence或45项Check门禁失败时，不返回可消费的部分Finding集合。Prompt预算继续使用政策2.0；Provider Prompt不超过6,000为目标内，6,001至7,000为软告警，超过7,000为硬失败。

## 16. 文件级计划

6.1以后预计新增/修改：Contract Python的`risk_models.py/playbooks.py/plan_builder.py`及隐藏接口；Framework的`risk_review.py/register.py`；`LlmRuntime.complete_with_usage()`；内部Stage状态映射；对应Framework和Contract测试。

不修改回调StageId、正式Finding/Evidence模型、Evidence Validator、Result Hash、Window、阶段5.1安全合并和Java。

## 17. 阶段6.0冻结结论

```text
PlanBuilder不是Framework Stage
只新增execute_risk_review_bundle一个隐藏模型执行单元
七Unit内部执行，五旧Stage保留为确定性Adapter
逐Finding按domain + check_code + category映射
映射缺失或多重命中直接失败
横向检查先生成候选，无候选0模型调用
一期正式检查默认REQUIRED
所有check_code显式返回状态
正常基础Reviewer Tool=0、模型调用=1
temperature=0、thinking=false、最多修复当前Unit一次
完整Token和TTFT按review_unit_id记录
阶段5.1、verify_evidence、finalize_review全部复用
公开协议、OpenAPI、状态、Attempt和Java不变
阶段6只读使用服务器固定101项IR/12 Window Fixture
FVA-005是唯一允许OTHER且risk_type固定
```

阶段6.0完成后停止，未经确认不得开始6.1。
