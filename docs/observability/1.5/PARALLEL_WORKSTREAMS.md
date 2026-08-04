# 日志与管理员观测平台 1.5 并行工作流登记

登记时间：2026-07-31  
执行环境：`afs2600151` 测试环境  
执行身份：`carper`（Git：`caranteecarper`）

## 1. 多仓基线

实际测试发布不是单一 Git 仓库，因此 `obs/00-baseline` 在 Java 与 Python 两个仓库中分别建立；两个基线共同构成同一平台基线。

| 代码域 | 只读发布源 | 基准分支/提交 | 基线分支 | 基线 worktree |
|---|---|---|---|---|
| Java | `/home/aituge/workspace/contract-review-code-dev/sources/java-main-aff8eb3b` | `main@aff8eb3b6043d44ede802554d87e3577d8875391` | `obs/00-baseline` | `/home/aituge/worktrees/obs-baseline/java` |
| Python | `/home/aituge/workspace/contract-review-code-dev/sources/framework-vconcurrent-beb880da` | `v.concurrent@beb880daf01a973d46ea10c68db14646b7c52515` | `obs/00-baseline` | `/home/aituge/worktrees/obs-baseline/python` |

当前测试 Compose Project 为 `contract-review-code-dev`。当前测试发布目录为：

`/home/aituge/workspace/contract-review-code-dev/releases/ours-20260731-713231b1-aff8eb3b-beb880da`

不得把 `/home/aituge/workspace/contract-review-dev` 下的旧源码副本或其中未提交修改作为本次基线。

## 2. 工作流、仓库与文件所有权

| 页面 | 分支 | worktree | 仓库 | 独占范围 |
|---:|---|---|---|---|
| 1 | `obs/10-java-ledger` | `/home/aituge/worktrees/obs-java-ledger` | Java | Java/MySQL 事件账本、Outbox/Inbox、迁移、事务和相关测试 |
| 2 | `obs/20-python-model` | `/home/aituge/worktrees/obs-python-model` | Python | 模型调用事件、投影、reconciliation、模型内部查询与相关测试 |
| 3 | `obs/30-python-task-security` | `/home/aituge/worktrees/obs-python-task-security` | Python | Task/Run 事件查询、Python 安全事件、highWatermark、内部 SSE 与相关测试 |
| 4 | `obs/40-java-query-api` | `/home/aituge/worktrees/obs-java-query-api` | Java | Java 只读管理员查询、Adapter、快照与时间线 |
| 5 | `obs/50-java-security-transport` | `/home/aituge/worktrees/obs-java-security-transport` | Java | Scope/Session/Context、安全传输、Runtime、Export、SSE 代理 |
| 6 | `obs/60-platform-test` | `/home/aituge/worktrees/obs-platform-test` | Python | 仅测试环境 `deploy/`、`infrastructure/`、观测组件和测试部署探针 |
| 7 | `obs/70-contract-tests` | `/home/aituge/worktrees/obs-contract-tests/java`、`/home/aituge/worktrees/obs-contract-tests/python` | Java + Python | 仅契约、生成烟测、静态检查、CI 和测试脚本；handoff 以 Python 仓库为登记主本 |
| 8 | `obs/90-integration` | `/home/aituge/worktrees/obs-integration/java`、`/home/aituge/worktrees/obs-integration/python` | Java + Python | 仅最终集成、共享入口接线、迁移合并、测试部署与验收 |

页面 7 和页面 8 使用两个仓库中的同名分支；每个仓库单独记录 Commit，不把两个仓库误写成一个 Commit。

## 3. 禁止并行修改的共享文件

页面 1—7 不得修改以下共享入口；确需变更时写入自己的 handoff，由页面 8 统一处理：

- Java 根 `pom.xml`、根依赖管理、应用启动类、全局 Spring/Security/Router/Bean 注册；
- Python 根 `pyproject.toml`、`poetry.lock`、应用初始化、根 Router、全局依赖注入、Alembic `env.py` 和并行 revision 合并；
- Compose 总入口、活动发布目录、共享 `.env`、凭据和密钥挂载；
- 冻结的 `docs/observability/1.5/01`—`07`、`README.md` 及两份 OpenAPI；
- 前端代码和正式环境的任何文件、网络、卷、数据库、镜像或容器。

页面 6 可以在自己的 Python worktree 中新增或修改明确属于测试环境的 `deploy/`、`infrastructure/` 和探针文件，但不得直接改动活动发布目录；活动测试发布只由页面 8 在完整验收前后记录并实施。

## 4. 测试环境边界

允许操作的 Compose Project/资源必须明确包含 `contract-review-code-dev`、`contract-review-dev`、`test` 或 `dev`。共享本地模型网络 `ai-model-runtime-net` 仅允许作为测试调用出口使用，不得修改正式路由。

已发现的隔离风险：测试 Framework/Proof 容器当前只读挂载了 `/home/aituge/workspace/AI-framework/worktrees/v.concurrent/aituge_model_config/secrets/` 下的密钥文件。不得读取或提交密钥；页面 6 负责给出测试专属凭据挂载方案，页面 8 验证隔离后再部署。

任何名称包含 `formal`、`prod`、`production` 的资源均不得查看详情、修改、重启或删除。禁止全局 Docker 清理、无 Project 限定的 Compose 操作以及跨环境数据库连接。

## 5. 合并顺序

页面 8 在 Java、Python 两个 `obs/90-integration` 分支中按适用仓库依次记录并集成：

1. `obs/10-java-ledger`
2. `obs/20-python-model`
3. `obs/30-python-task-security`
4. `obs/40-java-query-api`
5. `obs/50-java-security-transport`
6. `obs/60-platform-test`
7. `obs/70-contract-tests`

不得直接合并到 `main`、`master`、`develop` 或 `v.concurrent`，不得强推。最终完成后停在 `obs/90-integration` 等待人工评审。
