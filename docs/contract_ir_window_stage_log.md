# 合同 IR Window 改造阶段记录

本文件记录每个阶段的提交、自动测试、独立容器验收、测试页面结果和遗留问题。未通过当前阶段门禁时，不进入下一阶段。

## 阶段 0：工作区与设计冻结

- 状态：通过
- 基线：`origin/proof@7deb798`
- 分支：`feat/contract-ir-window-v1`
- 本地 Worktree：`D:\contract-ir-window-v1`
- 设计文档：`docs/contract_ir_window_refactor.md`
- 正式协议：顶层《合同审查一期 Java–Python–Framework 联合技术方案 v1.0（冻结稿）》
- 自动测试：`git diff --check` 通过，设计标题及五阶段清单检查通过
- 服务器独立环境：`contract-review-dev-framework-1`、`contract-review-dev-ai-contract-1`、PostgreSQL、Redis、Java 均运行；后续使用独立 `python-ir-window-source`，不修改现有脏 `python-source`
- 提交：`07b6c1b 文档：冻结合同IR窗口化改造方案`
- 远程分支：`origin/feat/contract-ir-window-v1`

## 阶段 1：Section Unit、Window Builder、Offset Map

- 状态：通过
- 开始前设计复读：已完整复读本设计，并复核联合冻结稿第 14～20、33～39 节；确认本阶段不修改 Java–Python 协议、Framework Stage、状态机和正式结果
- 实现：确定性 Section Unit、层级关系、子条款识别、4K/6K Window、超长 Block 非重叠切片、Offset Map、稳定 ID、100% 字符覆盖校验
- 代码提交：`46dfbcd 功能：实现合同IR结构窗口构建`
- 自动测试：服务器专用镜像内合同模块 `102 passed, 10 skipped`；其中新增窗口测试 6 项、测试页面测试 3 项
- 独立容器验收：镜像 `contract-ir-window-stage1:test`；容器 `contract-ir-window-stage1-ui`；仅绑定服务器 `127.0.0.1:19310`
- 测试页面验收：上传《服务外包协议之补充协议0829.docx》成功，识别 93 Block、12 Section、1 Window；Window 估算 2824 tokens；覆盖 93/93，无遗漏、无重叠
- 测试 Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage1-window-inspection.json`
- 遗留问题：该样本低于 4000 tokens，合理合并为单 Window；多 Window 和超长 Block 场景已由确定性测试覆盖，真实长合同留在阶段二/三继续验收

## 阶段 2：模型链路、LangExtract解析和单Window对齐

- 状态：通过
- 开始前设计复读：已完整复读本设计、阶段记录，并复核联合冻结稿第 14～20、33～39 节；确认本阶段仅实现单 Window 抽取，不替换正式 `extract_contract_ir` Stage，不修改 Java–Python DTO、状态机、Attempt 和回调协议
- 实现：Framework `LlmRuntime` 统一模型调用；`max_tokens=20000`、`temperature=0`；Framework 完整 JSON 提取；严格 Pydantic Schema；LangExtract `1.6.0` 精确对齐；Offset Map 映射回 Block `[start,end)`；禁止模糊/部分匹配；同一原文允许不同 IR 类别共享 Anchor；测试页面沿用阶段一容器和 `19310` 端口
- 代码提交：`5c0b668 功能：接入合同Window模型抽取与精确对齐`
- 自动测试：阶段二新增测试 `7 passed`；Framework 回归 `146 passed, 2 deselected`，排除项分别依赖未接入一次性容器的 Smoke 服务和 Redis；未排除执行时总计 `146 passed, 2 failed`，两项失败堆栈均为连接拒绝；Contract Python `102 passed, 10 skipped`
- 独立容器验收：镜像 `contract-ir-window-stage2-framework:test`、`contract-ir-window-stage2-contract:test`；页面继续使用 `contract-ir-window-stage1-ui` 和服务器本机 `127.0.0.1:19310`；模型助手为 `contract-ir-window-stage2-framework` 和 `127.0.0.1:19320`；助手只读挂载独立环境模型配置卷，未替换任何正式或独立主 Framework 容器
- 测试页面验收：真实合同解析仍为 93 Block、12 Section、1 Window、2824 估算 tokens、覆盖 93/93；合成 Window 通过真实 `deepseek-v4-pro` 抽取，耗时 21602 ms，生成 `OBLIGATION`、`PAYMENT` 两项；两项共享逐字原文，均精确映射到 `block-stage2-synthetic[7,30)`
- 测试 Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage2-window-inspection.json`、`stage2-synthetic-window.json`、`stage2-synthetic-window-extraction.json`
- 修复记录：首次模型助手因默认租户 `default` 找不到模型，改为独立环境真实租户 `0`；首次真实模型结果因 LangExtract 批量非重叠选择拒绝同句 `OBLIGATION + PAYMENT`，改为逐 Extraction 精确对齐并增加重叠语义回归测试
- 遗留问题：真实用户合同正文未发送到外部模型。用户虽已确认可发送，但执行环境的数据策略仍禁止向未标记可信的外部提供方导出该文件；因此该合同只完成 Parser/Window 验收，模型链路使用无真实主体和金额的合成合同验收。后续若使用一体机本地模型，可再补真实合同单 Window 对比，不影响阶段三实现

## 阶段 3：并发、IR映射、合并、Coverage和局部重试

- 状态：通过
- 开始前设计复读：已完整复读本设计、阶段记录以及联合冻结稿第 14～20、33～39 节；确认本阶段只建设 Window 内部编排，不切换正式 `extract_contract_ir` Stage，不修改 Java–Python API、状态机、Attempt、回调和 `schema_version=1.0`
- 实现：滚动 Worker 并发固定为 3；单 Window 最多执行两次；Schema、JSON、Alignment、执行异常及关键条款可疑空结果只重试当前 Window；二次失败则整个 Pipeline 失败且不返回残缺 IR；按 Block 字符区间验证 Primary Source、Section 和 Window 覆盖；将逐字 Extraction 确定性映射到现有 `ContractIrSemanticDelta`；生成 Source-grounded `anchor_id`、`item_id`、原文 Hash 和技术溯源字段；按 Window、字符位置、类别和稳定 Hash 排序；同类别、同逐字原文、同 Anchor 确定性去重；测试 API 和页面增加全 Window 并发抽取、逐 Window 耗时、重试、Coverage 和合并 IR 展示
- Prompt 补强：真实模型首轮把付款、交付和验收只归为 `OBLIGATION`，因此明确冻结“类别不互斥、不得用 OBLIGATION 代替专属类别”；修正后同一逐字原文可同时生成 `OBLIGATION + PAYMENT/DELIVERY/ACCEPTANCE`，并继续由逐 Extraction 精确对齐支持共享 Anchor
- 代码提交：`b187882 功能：实现合同IR窗口并发映射与合并`
- 自动测试：阶段三相关测试 `14 passed`；测试页面及 Window 构建测试 `9 passed`；Framework 全量回归 `154 passed, 2 deselected`，两项排除仍为一次性测试容器未接入 Smoke 服务与 Redis；Contract Python 全量 `102 passed, 10 skipped`；`git diff --check` 通过
- 独立容器验收：继续复用 `contract-ir-window-stage1-ui` 和服务器本机 `127.0.0.1:19310`，未新增测试前端；页面镜像升级为 `contract-ir-window-stage3-contract:test`；模型助手继续复用 `contract-ir-window-stage2-framework` 和 `127.0.0.1:19320`，只读挂载隔离源码及模型配置；正式容器和正式环境未修改
- 测试页面验收：页面已出现“并发 3 抽取全部 Window 并合并 IR”；四个无敏感信息的合成 Window 使用真实 `deepseek-v4-pro` 完成抽取，总耗时 39563 ms，单 Window 耗时分别为 16444、10162、19348、29383 ms；滚动并发使总耗时显著小于四项串行总和；模型调用 4 次、局部重试 0 次、4/4 Window、4/4 Section、4/4 Block 覆盖通过；合并后得到 12 项，覆盖 DELIVERY、OBLIGATION、PAYMENT、ACCEPTANCE、DATE、LIABILITY、RIGHT、TERMINATION、DISPUTE，全部精确映射回 Block `[char_start,char_end)`
- 测试 Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage3-synthetic-window-pipeline.json`；重复真实模型实验为 `stage3-synthetic-window-pipeline-repeat.json`
- 遗留问题：确定性 Mapper 对完全相同的已对齐模型输出会生成相同 IR Hash，且同类别、同原文、同 Anchor 的技术 `item_id` 不受模型 predicate/object 措辞影响；但两次真实模型调用仍会在谓词措辞、DATE/RIGHT 分类和 Extraction 边界上产生变化，因此完整 `semantic_ir_hash` 不保证跨独立模型调用一致。该质量问题必须在阶段四 Shadow Compare 中量化并决定采用更严格分类规范、结果缓存或其他稳定化措施，不能通过隐藏式语义规则伪造一致。真实用户合同仍只用于本地 Parser/Window 覆盖，没有发送给外部模型

## 阶段 3.1：300 Token Window与Contract IR关闭思考

- 状态：通过，等待用户在测试页面补充真实合同模型结果
- 开始前设计复读：已重新完整复读本设计、阶段记录以及联合冻结稿第 14～20、33～39 节；确认本次只调整 Contract IR Window，不修改风险判断等后续 Stage，不修改 Java–Python API、状态机、Attempt、回调和 `schema_version=1.0`
- 实现：Window默认软/硬上限统一为 300 估算 Token，所有 Primary Window 的 `estimated_tokens<=300`；超长条款继续按子条款、段落、表格行和句界确定性拆分；`WindowExtractionEngine`显式请求关闭思考；Framework `LlmRuntime.complete()`增加可选 `thinking_override`，仅显式覆盖且命中官方 `api.deepseek.com/deepseek-v4-*` 时发送 `thinking.type=disabled`，其他调用保持原配置和兼容字段
- 代码提交：`4ad506d 功能：合同IR窗口固定300 Token并关闭思考`
- 自动测试：针对性 `24 passed`；Framework 全量 `157 passed, 2 deselected`，两项排除仍为一次性容器未接入 Smoke 服务与 Redis；Contract Python 全量 `103 passed, 10 skipped`；`git diff --check`通过；隔离镜像没有安装 Ruff，未为静态检查新增依赖
- Window验收：原《服务外包协议之补充协议0829.docx》本地解析为 93 Block、12 Section、12 Window；单 Window 为 123～296 估算 Token，超过 300 的 Window 为 0；93/93 Block 覆盖有效，无遗漏、无重叠
- 模型验收：无敏感合成合同 4 Window、并发 3，通过真实 `deepseek-v4-pro`完成；单次模型耗时 1933～3737 ms；一个 Window 首轮 Schema 失败后局部重试成功；模型调用 5 次、局部重试 1 次；4/4 Window、4/4 Section、4/4 Block 覆盖通过；合并得到 11 项 IR，覆盖 OBLIGATION、PAYMENT、DELIVERY、ACCEPTANCE、LIABILITY、TERMINATION、DISPUTE
- 独立容器验收：继续复用 `contract-ir-window-stage1-ui` 和服务器本机 `127.0.0.1:19310`；页面镜像仍为 `contract-ir-window-stage3-contract:test`但已重建；模型助手仍为 `contract-ir-window-stage2-framework` 和 `127.0.0.1:19320`，已修正为优先加载只读 `/workspace` 新源码并继续使用原隔离模型配置卷；正式容器和正式环境未修改
- 测试 Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage31-window-inspection.json`、`stage31-synthetic-window-extraction.json`、`stage3-1-window-latency-matrix.json`、`stage3-1-window-quality-probe.json`、`stage3-1-window-latency-report.md`
- 遗留问题：执行环境不允许代理将完整用户合同发送给外部模型，因此自动验收只对真实合同完成 Parser/Window/Coverage，对真实模型使用无敏感合成合同；用户可在已更新的测试页面自行触发真实合同抽取并补充结果。精简 Prompt 虽更快但会漏掉 PAYMENT、DELIVERY、ACCEPTANCE 等专属类别，当前未采用

## 阶段 3.2：确定性规范化对齐与并发10

- 状态：通过
- 开始前设计复读：已重新完整复读本设计、阶段记录，并复核联合冻结稿中 Framework 执行、Contract IR、Evidence、错误与冻结结论；确认只改变内部 Window 对齐和调度，不修改 Java–Python API、状态机、Attempt、回调、Finding/Evidence DTO 及 `schema_version=1.0`
- 对齐实现：优先逐字符精确匹配；失败时对模型文本与 Window 原文执行 Unicode NFKC，忽略空白、换行、普通中英文标点和全半角差异，保留中文、数字、英文字母、金额和百分比等业务字符；禁止语义、拼音、同义词和编辑距离模糊匹配；匹配后通过索引映射还原原始 Block 的真实 `[char_start,char_end)` 与逐字 `quoted_text`；无法唯一定位返回 `ALIGNMENT_AMBIGUOUS`
- 并发实现：Window Pipeline 滚动并发由 3 固定为 10，Request、Result、测试 API 和测试页面保持一致；单 Window 两次局部尝试、失败不返回残缺 IR、Coverage 和确定性合并规则不变
- 测试页面：继续复用 `contract-ir-window-stage1-ui` 和服务器本机 `127.0.0.1:19310`；按钮及结果显示更新为并发 10；失败时直接显示失败 Window、两次 Attempt、错误码、错误信息、耗时及完整错误 JSON，不再只显示笼统 422
- 自动测试：对齐、Pipeline 与页面针对性测试分别组成 `20 passed` 和 `3 passed`；Framework 回归 `163 passed, 2 deselected`，两项排除仍为隔离容器未接入 Smoke 服务与 Redis；Contract Python 回归 `103 passed, 10 skipped`；`git diff --check` 通过
- 真实模型验收：12 个无敏感信息的合成 Window 使用真实 `deepseek-v4-pro`、固定并发 10 完成；总耗时 6416 ms；模型调用 13 次、局部重试 1 次；12/12 Window、Section、Block 全部成功且 Coverage 通过；合并得到 33 项 IR。唯一重试为 DATE 首轮缺少 predicate，第二轮按既有局部重试规则成功
- 测试 Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage32-concurrency10-request.json`、`stage32-concurrency10-response.json`
- 代码提交：`功能：合同IR支持规范化溯源并提升至并发10`
- 遗留问题：并发 10 已通过当前模型端点验证，但它会同时占用更多 HTTP 连接、在途响应缓冲和模型端点配额；若后续模型提供方收紧并发或一体机改为本地模型，应通过内部配置重新压测资源上限，不能直接假设 10 永远适合所有模型部署

## 阶段 3.3：DATE/AMOUNT 开放值规范化与 Span 关系绑定

- 状态：通过
- 开始前设计复读：已重新完整复核本设计、阶段记录及联合冻结稿；确认 Contract IR 属于 Python 内部技术模型，重要字段必须关联真实 Source Anchor，本阶段不修改 Java–Python API、状态机、Attempt、回调、Finding/Evidence DTO 或 `schema_version=1.0`
- 实现：不建立封闭的日期/金额子类型枚举，不按合同中的具体数字硬编码；模型已给出合法 DATE/AMOUNT 关系时原样保留；仅对缺失 `predicate` 的已对齐值执行 `TEMPORAL/NUMERIC` 基础族规范化，分别补充中性 `时间约束为/数值约束为`，并将逐字原文值写入 `object`
- 关系绑定：只依据当前 Window 的已对齐字符区间，依次选择唯一最小包含项或同一句唯一语义项；多个候选记为 `AMBIGUOUS`、没有候选记为 `UNBOUND`，两者都不猜关系、不做语义模糊匹配，也不触发整个 Window 重试；非 DATE/AMOUNT 类别缺少 `predicate` 仍按严格 Schema 失败
- 可观测性：Window Attempt 增加内部 `value_canonicalization_count`、`ambiguous_value_count`、`unbound_value_count`；测试页面显示总计和逐 Window 计数；这些字段只属于隔离测试结果，不进入正式 Contract IR 和跨服务协议
- 针对性测试：`30 passed`；覆盖任意工作日、月数、百分比、金额区间、模型合法关系保留、唯一包含绑定、同句唯一绑定、歧义不猜、无候选不造主体、非值类别仍失败以及 Pipeline 不新增模型调用
- Framework 回归：`173 passed, 1 deselected`；排除项是现有隔离容器未接 localhost Redis 的会话压缩现场测试，另一个依赖未启动 Smoke 服务的 live 文件按既有方式忽略；均与本次改动无关
- Contract Python 回归：`103 passed, 10 skipped`
- 真实模型验收：复用阶段 3.2 的 12 个无敏感合成 Window 请求，真实 `deepseek-v4-pro`、并发 10；总耗时 `6507 ms`，模型调用 `12` 次，局部重试 `0` 次，Coverage `12/12`，合并得到 `42` 项 IR；本轮模型直接返回完整 DATE 关系，因此规范化计数为 0，证明正常合法输出不会被覆盖，缺字段兜底路径由确定性测试覆盖
- 隔离容器：继续复用 `contract-ir-window-stage1-ui`、`contract-ir-window-stage2-framework`、服务器本机 `127.0.0.1:19310/19320`；只重建测试 UI 镜像并重启现有隔离容器，正式容器和正式环境未修改
- 测试 Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage33-value-canonicalization-response.json`
- 代码提交：`功能：规范化合同日期金额值关系`
- 遗留边界：内部规范化只保证值实体结构完整和真实溯源，不替代后续跨 Window 关系判断；歧义/未绑定计数将在阶段 4 Shadow Compare 中继续量化

### 阶段 3.4：主体上下文接线

- 状态：已完成
- 目的：补齐测试旁路与正式链路之间的主体上下文差异；正式链路原有 `resolve_parties` 不变，本阶段只把它的类型化投影注入 Window 的 `context_only`
- 输入：`party_a_name`、`party_b_name`、`perspective`、`contract_type`、固定一期 `review_attitude=NEUTRAL`
- 确定性派生：根据 `perspective` 计算 `our_party` 和 `counterparty`；甲乙方同名或空白由请求校验拒绝
- 溯源边界：主体上下文只写 `context_text`，不写 `source_text` 和 Offset Map；Extractor、Alignment、Anchor 仍只能引用 `source_text`
- 测试页面：增加甲方、乙方、立场和合同类型输入；单 Window 与全量抽取结果都显示已解析的主体上下文
- 针对性测试：Framework Window `31 passed`；Contract 测试页面 `3 passed`
- 真实模型验收：复用 12 个无敏感合成 Window，真实模型、并发 10；总耗时 `5814 ms`，模型调用 `12` 次，局部重试 `0`，Coverage `12/12`；注入的“甲方测试单位/乙方测试单位”在所有证据 `quoted_text` 中出现 `0` 次
- 测试 Artifact：`/home/aituge/workspace/contract-review-dev/test-artifacts/stage4a-party-context-response.json`
- 影响范围：只修改功能分支和 `contract-ir-window-*` 隔离容器；未修改正式 Pipeline、公开 DTO、OpenAPI、数据库或正式容器
- 代码提交：`功能：为合同Window注入主体上下文`

## 阶段 4：Legacy/Window Shadow Compare

- 状态：未开始
- 开始前设计复读：未完成
- 代码提交：
- 自动测试：
- 独立容器验收：
- 测试页面验收：
- 测试 Artifact：
- 遗留问题：

## 阶段 5：完整Finding/Evidence回归

- 状态：未开始
- 开始前设计复读：未完成
- 代码提交：
- 自动测试：
- 独立容器验收：
- 测试页面验收：
- 测试 Artifact：
- 遗留问题：
