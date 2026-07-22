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
  -> Window Extractor（最多并发 3）
  -> Framework LlmRuntime + 当前 CONTRACT_MODEL_ID
  -> LangExtract 结果解析及 Source Alignment
  -> ContractIrMapper 生成技术字段
  -> DeterministicIrMerger
  -> CoverageChecker / 局部重试
  -> 对外唯一 extract_contract_ir 结果
  -> Contract Python 终检并持久化
```

LangExtract 不作为独立服务，不接管 Framework 的模型配置、任务、并发、重试和日志。它只提供结构化 Extraction 数据、结果解析和原文对齐能力。

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
- 软上限初始为约 4,000 模型 Token；
- 硬上限初始为约 6,000 模型 Token；
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
- [ ] 阶段 2：Framework 模型链路、LangExtract 解析和单 Window 对齐；
- [ ] 阶段 3：并发 3、IR Mapper、合并、Coverage、局部重试；
- [ ] 阶段 4：Legacy/Window Shadow Compare；
- [ ] 阶段 5：独立测试环境完整 Finding/Evidence 回归。

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
