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

- 状态：未开始
- 开始前设计复读：未完成
- 代码提交：
- 自动测试：
- 独立容器验收：
- 测试页面验收：
- 测试 Artifact：
- 遗留问题：

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
