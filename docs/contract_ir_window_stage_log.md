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
- 提交：待生成
- 远程分支：待推送

## 阶段 1：Section Unit、Window Builder、Offset Map

- 状态：未开始
- 开始前设计复读：未完成
- 代码提交：
- 自动测试：
- 独立容器验收：
- 测试页面验收：
- 测试 Artifact：
- 遗留问题：

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
