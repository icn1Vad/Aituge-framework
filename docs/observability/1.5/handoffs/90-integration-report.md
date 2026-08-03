# Page 8 最新代码集成与验收报告

报告日期：2026-08-04
执行主机：afs2600151
执行用户：aituge
目标分支：obs/90-integration-latest
状态：集成验证通过；尚未部署到测试业务容器

## 1. 范围与不可破坏边界

本轮只在服务器隔离工作树执行：

- Java：`/home/aituge/worktrees/obs-latest-java`，基线 `57d0220bd750289daf58c28b80f6bae7b5b57069`。
- Python：`/home/aituge/worktrees/obs-latest-python`，基线 `e23fe3342b20d411229c92d58211f8f046d0f5fd`。
- 两棵工作树分支均为 `obs/90-integration-latest`，当前修改尚未提交、未 push、未合并 main/v.concurrent。
- 主源码树 `/home/aituge/workspace/continew-dev/source` 与 `/home/aituge/workspace/AI-framework/worktrees/v.concurrent` 未被本轮写入。
- 未修改前端；管理员观测页仍是现有 mock/硬编码页面，前端 API 代理接入不在 Page 8 代码范围内。
- 未重启、重建、删除或写入正式 Java、Framework、Proof、数据库及正式网络；未对活动业务数据库执行迁移或写入。
- 所有测试、临时脚本和测试容器均在一体机服务器完成，没有向本地 D 盘下载源码、镜像或测试数据。

## 2. Java 最新代码集成

已接入并通过定向验证：

- 业务事件账本、Outbox/Inbox、模型/安全/运行观测边界和管理员观测配置。
- 合同审查任务创建与 Outbox 在同一业务事务内写入；Outbox 引用固定的 `reviewSourceVersionId`，不被当前版本指针变更误导。
- Outbox 重放/恢复按源版本事实校验，避免重复派发或错误状态冲突。
- `application.yml` 仅新增 `observability.*` 配置，默认全部关闭；同时关闭请求/响应 Header、Body 写入访问日志，避免凭据、合同正文和能力型 Token 进入日志。数据库地址、模型配置、内部业务 Token 和正式参数未改。
- Liquibase 只新增观测账本 changelog include；未执行到活动数据库。

Java 验证结果：

- `continew-contract` 及依赖模块编译：BUILD SUCCESS。
- 合同模块完整测试：186 passed，0 failed，0 error。
- 观测相关 business 定向测试：176 passed，14 skipped，0 failed；14 项为未接入 Redis 的既有集成测试，未计为通过。
- server 观测/签名/secret 测试：15 passed，10 skipped，0 failed；10 项为未提供 MySQL 的集成测试，未计为通过。
- `git diff --check`：通过。

## 3. Python 最新代码集成

已将模型调用观测、任务/安全观测、合同修订链路和 Proof 模型边界接到最新 v.concurrent 工作树。测试桩仅补齐最新生产响应 DTO 的字段（`defer_terminal`、TTFT、调用关联 ID 和终结器），没有放宽生产校验。

Python 验证结果：

- 观测契约、模型观测和安全探针套件：233 passed，3 skipped，2 strict xfailed，0 failed。
- 最新业务/合同/模型/Proof 定向套件：85 passed，3 skipped，0 failed。
- Bash 语法、Python AST、组合配置/采集器校验、Ruff/格式差异和 `git diff --check`：通过。
- 3 个 skipped 是未提供外部数据库/服务的集成条件；没有把它们写成真实生产验证。
- 曾出现的 1 个失败是隔离测试容器 `/tmp` 使用 `noexec` 导致合成 guard 测试无法执行，改为测试专用 exec tmpfs 后通过；不是源码语义失败。

## 4. 当前测试观测栈状态

已有 Page 8 观测组件仍在测试观测项目中运行，当前可见且 healthy/up 的 7 个容器为：

- `contract-review-code-dev-observability-grafana-1`
- `contract-review-code-dev-observability-prometheus-1`
- `contract-review-code-dev-observability-otel-collector-1`
- `contract-review-code-dev-observability-alloy-1`
- `contract-review-code-dev-observability-loki-1`
- `contract-review-code-dev-observability-tempo-1`
- `contract-review-code-dev-observability-alertmanager-1`

本轮没有把最新 Java/Python 工作树构建成业务镜像，也没有替换这些业务容器；因此“代码测试通过”不等于“最新代码已经在线运行”。正式观测链路未改。

## 5. 尚未关闭的边界

- 宿主 capture systemd 服务尚未安装；安装需要用户亲自完成 root/sudo 认证，本轮没有代填密码或提权。
- OTel Collector 对带 Span Link 属性的安全清洗能力仍不足，异步完整 Span Link 保持关闭；通过 `taskId/runId` 和事件账本关联不影响业务链路。
- MySQL、Redis、PostgreSQL 的真实活动环境写入验收未执行；本轮使用隔离容器或无依赖跳过。
- 前端管理员观测页面未接真实接口，后续需要单独的前端任务和 Java API 代理接入。

## 6. 清理与安全证据

- 本轮带 `codex.task=obs90-*` 标签的临时测试容器已清理；没有执行 `docker prune`、Compose down、全局网络清理或命名业务 volume 删除。
- 服务器临时测试脚本和补丁文件已删除；没有保留密钥、Cookie、浏览器状态、数据库文件或构建产物到报告目录。
- 源码变更中没有发现 secrets、`.key`、数据库文件、缓存目录、`node_modules` 或 Python 字节码。
- 代码与观测组件均未 push 或合并；下一步若部署，应先审查并提交两棵 `obs/90-integration-latest` 分支，再生成新的测试 release，保留现有测试数据库/OnlyOffice/模型 secret 挂载边界。

## 7. 验收结论

当前结论为：**最新 Java/Python 源码在服务器隔离环境中通过本轮 Page 8 定向验收，可以进入提交前代码审查；尚不能声称已部署、已完成活动测试网络真实业务验收或已验证正式链路。**
