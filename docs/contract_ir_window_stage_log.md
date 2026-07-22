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
