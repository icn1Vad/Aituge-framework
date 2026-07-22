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

- 状态：未开始
- 开始前设计复读：未完成
- 代码提交：
- 自动测试：
- 独立容器验收：
- 测试页面验收：
- 测试 Artifact：
- 遗留问题：

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
