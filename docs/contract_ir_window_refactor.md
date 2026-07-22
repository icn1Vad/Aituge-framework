# 合同 IR Window 抽取改造设计

状态：阶段实施依据  
基线：`proof@7deb798`  
分支：`feat/contract-ir-window-v1`  
协议约束：Java–Python `schema_version=1.0` 保持不变

## 1. 目标与边界

本次只改造 Contract IR 的内部生成方式：将五个按类别划分、反复读取合同全文的 ReAct Agent，替换为按合同结构建立 Window、逐 Window 一次抽取全部 IR 类别、确定性合并的流水线。

保持不变：

- Java、前端和 Java–Python API；
- Python 技术状态及 Java 业务状态；
- 对外 `extract_contract_ir` / `IR_EXTRACTION` 语义；
- `ContractIR`、Finding、Evidence 和 Result Hash 的冻结协议；
- Framework Task、Run、Attempt、回调和孤儿恢复机制；
- `relationships=[]`、`bounding_boxes=[]` 的 `schema_version=1.0` 约束。

本次不建设 DOCX 页码和 Bounding Box。DOCX 原文定位以 `block_id + [char_start,char_end)` 为准。

## 2. 目标链路

```text
Contract Python 持久化 Block
  -> Framework Contract Capability 一次读取全部 Block
  -> SectionUnitBuilder
  -> SectionWindowBuilder
  -> Window Extractor（最多并发 10）
  -> Framework LlmRuntime + 当前 CONTRACT_MODEL_ID
  -> Framework 完整 JSON 解析 + 严格 Pydantic Schema
  -> LangExtract 1.6.0 精确 Source Alignment
  -> ContractIrMapper 生成技术字段
  -> DeterministicIrMerger
  -> CoverageChecker / 局部重试
  -> 对外唯一 extract_contract_ir 结果
  -> Contract Python 终检并持久化
```

LangExtract 不作为独立服务，不接管 Framework 的模型配置、任务、并发、重试和日志，也不直接调用模型。模型输出继续由 Framework 的 `parse_json_output` 提取完整 JSON，再由严格 Pydantic Schema 校验；LangExtract 只提供 Extraction 数据模型和精确原文对齐能力。

Contract IR Window 调用固定关闭 DeepSeek V4 思考模式，以受控抽取方式生成最小语义；后续风险判断 Stage 是否启用思考仍由各自模型配置决定，本设置不得改变其他 Stage。

## 3. Section Unit 规则

按现有有序 Block 确定性建层，不使用 LLM 切分。

开启新 Section：

- 章、节；
- `第X条`；
- Word Heading；
- 其他已明确识别的顶层条款标题。

记录为子结构：

- `1.1`、`1.2`；
- `（一）`、`（二）`；
- `1）`、`2）`；
- 其他稳定的子条款编号。

表格规则：

- 同一表格归属前一个有效 Section；
- 表头和表体默认不拆；
- 超过硬限制时才按完整行拆；
- 保留 `table_no`、`row_no` 和原 Block 引用。

第一条前的标题、主体和鉴于内容组成 `PREAMBLE`。

## 4. Window 规则

- 完整条款优先保持在同一 Primary Window；
- 相邻小 Section 可合并，但不跨大章边界；
- Contract IR Window 的软上限和硬上限均固定为约 300 模型 Token；
- `estimated_tokens`不得超过 300；超长条款仍按子条款、段落、完整表格行和句子确定性拆分；
- 超大 Section 依次按子条款、段落、完整表格行、句子拆分；
- 普通有效 Block 必须且只能属于一个 Primary Window；
- 单个 Block 本身超过硬限制时，允许按句界拆成多个不重叠 Source Slice；这些 Slice 的字符区间必须首尾相接并完整覆盖原 Block，不能遗漏或重叠；
- 一期正文不做普通 Overlap，防止重复抽取；
- 合同双方、视角、合同类型、上级标题等以 `context_only` 提供，不能成为 SourceAnchor。

每个 Window 保存：

```text
window_id
sequence_no
section_ids
heading_path
clause_nos
primary_block_ids
source_text
context_text
offset_map
estimated_tokens
```

## 5. Window 抽取协议

每个 Window 一次抽取全部适用类别：

```text
DEFINITION
RIGHT
OBLIGATION
PROHIBITION
PAYMENT
DELIVERY
ACCEPTANCE
LIABILITY
TERMINATION
CONFIDENTIALITY
INTELLECTUAL_PROPERTY
DISPUTE
DATE
AMOUNT
```

模型只输出最小语义：

```text
extraction_class
extraction_text
subject / predicate / object
term / meaning
referenced_clause_nos
```

模型禁止输出过程说明、Markdown、技术 ID、Block ID、字符位置和 Hash；禁止调用工具；`extraction_text` 必须来自当前 `source_text`。

同一原文允许同时产生不同语义类别，例如一条付款义务同时属于 `OBLIGATION` 和 `PAYMENT`。对齐按单条 Extraction 执行，不得因为第三方批量对齐器的非重叠选择而删除合法语义。

原文定位采用确定性字面规范化匹配：先尝试逐字符精确匹配；失败后对模型文本和 Window 原文同时执行 Unicode NFKC，忽略空白、换行、普通中英文标点以及全半角形式差异，再做规范化后的精确匹配。中文、数字、英文字母、金额和百分比等业务有效字符必须保留；不使用语义相似、同义词、拼音或编辑距离模糊匹配。匹配成功后必须通过索引映射还原为原始 Block 的真实 `[char_start,char_end)` 和逐字 `quoted_text`。同一 Window 中无法唯一定位时返回 `ALIGNMENT_AMBIGUOUS`，不得猜测第一个位置。

DATE 和 AMOUNT 不再建立封闭的日期/金额子类型枚举，也不按具体数值写业务规则。模型已经输出合法 `predicate` 时完整保留；只有 `predicate` 缺失时，才在完成 Source Alignment 后执行确定性结构规范化：

```text
DATE   -> TEMPORAL -> predicate=时间约束为
AMOUNT -> NUMERIC  -> predicate=数值约束为
object -> 已对齐的逐字 extraction_text
```

关系绑定只使用当前 Window 已对齐的 Source Span：优先选择唯一包含该值且范围最小的语义项；没有包含关系时，只接受同一句中唯一的语义项。多个候选记为 `AMBIGUOUS`，没有候选记为 `UNBOUND`，均不得按关键词或语义相似度猜测。歧义或未绑定值仍保留原文锚点和中性结构，不触发整个 Window 的第二次模型调用；规范化、歧义和未绑定计数只进入内部测试诊断，不改变公开 Contract IR、Java–Python DTO 或 `schema_version=1.0`。除 DATE/AMOUNT 外的类别缺少 `predicate` 仍然按严格 Schema 失败。

## 6. 确定性字段

由代码根据数据库上下文、Window Offset Map 和对齐结果生成：

```text
item_id
anchor_id
document_id
generation_id
window_id
section_id
block_id
char_start
char_end
source_order
content_hash
quoted_text_hash
parser_version
ir_version
created_at
clause_no
heading_path
```

正式锚点必须映射回当前 Generation 的真实 Block。一个 Extraction 跨多个 Block 时生成多个 SourceAnchor。

## 7. 合并、覆盖与失败

合并排序固定为：

```text
window.sequence_no
-> extraction.char_start
-> extraction_class
-> stable_hash
```

只确定性合并精确重复和同 Anchor 的规范化重复；不使用语义向量激进删除。

Coverage 必须确认：

- 所有有效 Block 已进入 Section；
- 所有 Section 已进入 Primary Window；
- 所有 Primary Window 已成功处理；
- 无遗漏、无重复 Primary 归属；
- 关键标题 Window 的空结果经过复查。

每个 Window 最多执行两次。Schema、JSON、Alignment、截断或可疑空结果失败时只重试该 Window。第二次仍失败则整个 IR Stage 失败，不返回残缺成功结果。

## 8. 跨 Window 关系

Window Extractor 不读取其他 Window。全部局部 IR 合并后，Python根据显式条款引用、相同主体/客体/金额/期限及典型业务关系生成 Relation Candidate。后续关系判断只接收相关 IR 对及对应原文，不重新读取全文。

`schema_version=1.0` 的公开 `relationships` 仍固定为空；内部关系只用于生成 Finding。

## 9. 兼容与回滚

保留内部引擎开关：

```text
CONTRACT_IR_ENGINE=legacy
CONTRACT_IR_ENGINE=window
```

先进行 Shadow Compare，新引擎结果保存为测试 Artifact，不覆盖正式 IR。质量验收后只在独立测试环境切换 `window`；稳定一个版本周期后再评估删除旧五个 IR Agent。

Shadow Compare 只按“相同 IR 类别 + 原文 Anchor”衡量来源一致性，不把 Legacy 当作标准答案：先匹配完全相同区间，再一对一匹配同 Block 的重叠区间，剩余项分别列为 Legacy 独有和 Window 独有。跨 Parse Generation 时，只有 `block_no + UTF-8原文SHA-256` 同时一致的 Block 才建立确定性对应；禁止按语义、相似文本或模型判断映射 Block。原始一致率只用于发现差异，不能表述为法律准确率。

对付款、验收和争议三类强指示条款增加类别完整性门。明确出现对应类别指示而模型未返回该类别时，只重试当前 Window 一次；第二次仍缺失则整个 IR Stage 失败。该门只检查抽取分类是否完整，不判断合同风险，不根据具体金额或特定合同写规则。

## 10. 独立测试页面

测试页面仅部署在合同审查独立测试环境，不进入正式前端。页面至少提供：

- 选择/上传测试合同；
- 选择 `PARTY_A` 或 `PARTY_B`；
- 触发 Section/Window 构建；
- 查看 Section Tree、Window 列表和每个 Window 的 Block 范围；
- 查看 Offset Map 与原文定位；
- 触发单 Window 抽取；
- 查看 Extraction、Alignment、映射后的 IR；
- 触发完整测试审查并查看阶段耗时、Token、重试和错误；
- 对比 Legacy 与 Window IR。

其中主体信息复用正式 `resolve_parties` Artifact 的类型化投影，并且只能作为 `context_only` 注入；`our_party`、`counterparty` 由所选立场确定性计算。主体上下文不得进入 `source_text`、Offset Map、Extraction 原文或 Evidence。

该页面只能调用测试环境接口，不保存用户正式业务数据，不暴露密钥和完整 Prompt。

## 11. 阶段门禁

每个阶段开始前必须重新阅读本文件及顶层联合冻结稿的第 14～20、33～39 节。每阶段必须依次完成：

1. 实现；
2. 将未提交代码临时同步到服务器隔离工作树；
3. 在服务器独立环境构建并运行单元/集成测试；
4. 通过测试页面或接口验收并保存 Artifact；
5. 中文 Git Commit；
6. 推送 `feat/contract-ir-window-v1`；
7. 在阶段记录中登记提交与验收结果；
8. 验收通过后才进入下一阶段。

阶段清单：

- [x] 阶段 1：Section Unit、Window Builder、Offset Map；
- [x] 阶段 2：Framework 模型链路、LangExtract 解析和单 Window 对齐；
- [x] 阶段 3：并发调度（当前上限 10）、IR Mapper、合并、Coverage、局部重试；
- [x] 阶段 3.4：测试旁路主体上下文接线与溯源隔离；
- [x] 阶段 4：Legacy/Window Shadow Compare；
- [x] 阶段 5：独立测试环境完整 Finding/Evidence 回归；
- [x] 阶段 5.1：跨 Stage Finding 三态判重与确定性合并（单份已保存结果重放通过；进入删除 Legacy 前仍需多合同回归）。

## 12. 验收指标

```text
Schema 合法率 = 100%
正式 Anchor 有效率 = 100%
Block 字符区间 Primary Window 覆盖率 = 100%
相同输入重复执行 IR Hash 一致
关键 IR 人工召回不低于 Legacy
当前 93 Block 合同 IR 阶段目标 <= 180 秒，优化目标 <= 120 秒
IR Prompt Token 相对当前约 155 万至少下降 70%
```

完整审查达到约 3 分钟还需要下一阶段改造风险判断层，本次 IR 改造不对完整审查总耗时作不现实承诺。

## 13. 跨 Stage Finding 语义合并

五个风险审查 Stage 仍然并行产出各自的 `Finding` 和 `EvidenceCandidate`。在现有
`verify_evidence` 网关内部增加隐藏的判重步骤，不新增公开 Stage，不改变 Java–Python
API、OpenAPI、状态机、公开 Finding/Evidence DTO 或 `schema_version=1.0`。

执行顺序固定为：

```text
五个风险 Stage Artifact
→ Python 确定性筛选候选 Finding 对
→ Framework LlmRuntime 一次非思考分类（超预算时按完整候选组分批）
→ 输出 SAME_RISK / RELATED_DISTINCT / DISTINCT
→ Contract Python 校验引用和 pair_id
→ 确定性选择保留项、合并 Evidence、再次去重
→ 现有 Evidence materialization 与 finalize_review
```

模型只接收候选对的 ID、类别、风险等级、标题、问题摘要和证据位置摘要；不接收完整合同、
完整 IR、修改建议或历史对话。模型不得创建或删除 Finding，不得改写审查结论，不得修改
风险等级、甲乙方、立场或 Evidence。正常规模只调用一次；严格 JSON 或候选覆盖校验失败时
最多修复一次，仍失败则返回 `SKIPPED`，所有原 Finding 原样保留，不能使审查任务失败。

第一版自动合并边界：

- 只有同类别、同立场、同我方/相对方且模型判为 `SAME_RISK` 才允许合并；
- 跨类别的 `SAME_RISK` 只记录，不自动删除，避免把相关但法律性质不同的风险合并；
- 一个合并组中任意两项都必须有 `SAME_RISK` 判断，不依赖不安全的传递推断；
- 保留项按风险等级、描述完整度和稳定序列确定，风险等级只能保持或提高；
- Evidence 先求并集，再按类型、Block、字符区间或缺失范围确定性去重；
- 未进入安全合并组的 Finding 必须全部保留。

候选引用使用 `artifact_type + source_finding_id`，模型只回传稳定 `pair_id` 和三态关系。
Contract Python 在命名空间转换后重新校验引用，旧 Stage 的局部 ID 不能误指向其他 Stage 的
Finding。公开结果仍只保存最终 Finding/Evidence，不暴露内部候选对或模型分类过程。
