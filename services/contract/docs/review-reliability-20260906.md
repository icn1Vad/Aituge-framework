# 审查可靠性修复：代码与 13009 部署验证记录

日期：2026-09-06。分支：`feature/legal-knowledge-graph-e2e`。

以下前半部分是首次离线验证时的记录；2026-09-06 晚间已按用户授权部署到 13009，最新状态见文末“13009 部署补充”。未提交或推送本轮改动。

## 四项改动

1. **证据修复**：不再用模型填错的编号过滤修复上下文。财务、通用审查和候选证据重选都提供原始目录及原文，保留正确配对候选。引用重绑允许纠正证据类型和编号；不存在的编号、跨原文错误配对仍被拒绝。缺少一个编号时仅允许唯一匹配自动补齐，多解不能随意取第一个。
2. **同一问题联合审查**：区分“多检查打包窗口”和“一个检查项的完整事实窗口”。单项相关事实在安全窗口内整项交给模型，避免同一验收问题分成 95 / 8 段后各自作全文判断。真正超长仍分片的结果只能作为观察，局部 ABSENCE 不作为已验证 Finding；保留诊断或 `deferred_findings`，记录待复核，不冒充无风险。现有任务记录继续保留期望/已提供 IR、原文锚点和范围完整性。
3. **校验与复核顺序**：先处理固定付款立场，再处理适用性、范围和保障条件。已确认履约后付款却被判为履约前付款时，针对该检查项重新复核。分开格式修复、证据重绑和业务复核；业务复核允许同步纠正标题、说明和结论，未涉及的检查项保持原样。漏答已分配检查项可以补齐，不能新增未分配检查。修复前后原始结果和错误继续留在诊断记录中。
4. **停机恢复与页面状态**：持有有效版本化数据库租约的 Worker 不再等待独立的旧 Redis 锁。租约过期、丢失或版本不符不得执行；同进程同租约重复执行被拦截。拿到任务但未执行时返回空闲结果并退避，避免空转。Java 不再随轮询次数增加进度；运行中的 direct `finalize_review` 显示为风险审查。页面获取状态失败后显示断线及最后已知状态，而不是声称后台仍在执行。

页面新增的 15 秒仅限制单次“读取任务状态”的 HTTP 请求，不限制合同审查执行时间，也不恢复之前移除的主体识别 5 秒限制。

## 验证

离线运行器禁止 Python 外网 socket，使用伪造模型完成结果和临时 SQLite，不配置生产数据库或 API 凭据。

```powershell
$env:PYTHONUTF8='1'
& 'E:\ProofSpaceLegalKG\review-offline-venv\Scripts\python.exe' `
  'C:\Users\Admin\Documents\Codex\workspaces\proofspace-legal-knowledge-graph-e2e\output\run_review_reliability_suite.py'
```

最终报告目录：`E:\ProofSpaceLegalKG\review-reliability-20260906\191649`。

- Python：462 passed，44 skipped，44 warnings。
- 前端：5 个文件，48 passed。
- TypeScript：`tsc --noEmit --incremental false` 通过。
- 三个仓库 `git diff --check` 通过。
- 模型调用与 embedding 调用：均为 0。

新增失败形态回放位于 `tests/test_review_reliability_regressions.py`：错类型、缺编号、多解编号、合法但错误配对、未知原文、目标与非目标项隔离、漏项修复、空说明、部分缺失隔离、付款先后矛盾、租约恢复/过期/重复执行及空转退避。这里不是声称已重放完整 SIG-003 生产合同；生产诊断仍在停止的 Docker 数据卷内。

44 个跳过项：42 个需要固定风险样本/历史候选输出，1 个需要明确配置的临时 PostgreSQL，1 个需要规则快照。没有把跳过算作通过。Windows 不验证 Linux 的 0600/0700 模式位；Linux 分支的权限断言仍保留。

## 尚未验证和边界

- Docker 当前未运行，13009 尚未更新；Java 完整 Maven 编译、Linux 容器回归、PostgreSQL 并发恢复、真实合同及公网页面复测均未完成。不能据此声称线上报错已经消失。
- 单项联合窗口默认估计 24,000 个业务上下文 token；超过窗口不会截掉原文，也不会把各片局部无风险自动等同于全局无风险。目前仍记录待联合复核，不宣称无限长合同的自动综合已经完成。
- 新锁机制修复的是有效 DB 租约被旧 Redis 锁阻塞的问题，不是端到端模型请求 exactly-once。完成批次的跨进程费用去重缓存不在本轮实现范围内。
- 额外读到的既有问题：`legal_evidence/prompting.py` 仍以历史 7,000 预算计算法律目录余量；目录可能被省略而 Evidence ID 仍保留。此机制本轮未修改，不能仅凭引用 ID 存在就认定模型读过法条。后续上线验收需要单独检查法律目录保留情况。
- 本轮没有关闭真实原文绑定、立场、安全租约和范围完整性校验；无法可靠修复的结果仍明确失败或待复核，不伪装成功。

上线前先检查 Docker、活动任务和模型调用，再做小范围镜像更新；不启动整套收费审查作为部署探活，不动 13005 / 13007。

## 13009 部署补充（2026-09-06 晚间）

已完成，不只是构建镜像：

- Docker 29.4.3 启动成功。启动前将两个仅含零字节套接字的临时目录原样重命名留档（后缀 `stale-20260906-214345`），没有恢复出厂、清空 Docker 数据或重建 13005/13007。
- 启动后暂挂 13009 旧 Framework API/Worker，确认 MySQL 和 Framework PostgreSQL 均没有活动审查后才替换服务。原有 72 条 Java 审查、153 条 Framework 运行记录的 ID/状态摘要在部署前后一致，没有重新发起审查。
- 只更新 `proofspace-legal-e2e` 项目的 `framework`、`framework-worker`、`ai-contract`、`java`、`frontend`。`framework_http_gateway.py` 实际也由 ai-contract 加载，因此该组件一并更新。
- 五个容器均运行，具备 healthcheck 的四个容器 healthy，Worker running，重启次数均为 0；restart policy 保持 `unless-stopped`。
- Framework API/Worker 的 9 个修改文件，以及 ai-contract 的网关文件，共 19 组容器内源码 SHA-256 与工作区一致。
- 更新目标的挂载/网络保持不变；其余 13005/13007 及本项目的数据库、OCR、OnlyOffice、网关等容器 ID、镜像、启动时间、重启计数、挂载和网络均与启动后的部署前快照相同。Docker Mounts 返回顺序不稳定，比较时按挂载目标排序，不忽略真实差异。
- 13005、13007、13009 首页均 HTTP 200；13009 `/contract-review` HTTP 200，10 个并发首页轻量请求全部 200。
- 公网 `https://vettingtest.cortexdata.cn/contract-review` 使用浏览器常用 User-Agent 请求复核为 HTTP 200。初次默认 Python User-Agent 请求为 403；没有修改 Cloudflare 配置。未声称已经验收登录后的 OnlyOffice 或真实合同审查。

### 已部署镜像

镜像标签统一为 `review-reliability-20260906`，全部本地构建，无远程业务镜像拉取。

| 本地镜像 | Docker image ID |
| --- | --- |
| `local/proofspace-legal-framework`（API/Worker 共用） | `sha256:fbfd79d6748d18568273462ad30e470bac0faad67fbd1a0629c68ad423e21b1a` |
| `local/proofspace-legal-contract` | `sha256:443d582bc35bf32b480a5278e3012ec32593c75e85ffc4977e1bb7079dfab9d4` |
| `local/proofspace-legal-java` | `sha256:01721403d2bb59221139bc78f7f1d63cac70981374ed4dc5500b4aa395a3d8d0` |
| `local/proofspace-legal-frontend` | `sha256:65699e30ef8bbb78b8e93e8e8b9338ada6e69140e8e6d15441ba6d7d970482c1` |

Compose 使用 deployment 仓库 `environments/legal-evidence-e2e/` 中已有的 `compose.yml`、`rules-preview.override.yml`、`office.override.yml`；只更新 rules-preview override 的五个镜像引用。

### 本次新增验证

- Java：容器内 JDK 17/Maven 离线编译及完整服务打包成功；4 个针对性测试类共 **46 passed，0 failed，0 skipped**，包括 1,000 次不变轮询不制造进度的回归。
- Linux：相同回归集 **462 passed、44 skipped**；禁止网络，源码只读挂载，没有生产 API 凭据。
- PostgreSQL：在单独的 internal Docker 网络、临时 tmpfs PostgreSQL 中，五租户并发领取测试 **1 passed**。补验了上述 Linux 集中跳过的 PostgreSQL 项；其余 43 项专用样本/快照仍未补齐。测试容器、网络及仅位于内存中的测试库已清理，未接触生产数据库。
- 前端生产构建成功（已有未使用 `RefreshCw` import 的 lint warning 不阻止构建）；此前 48 项前端测试和 TypeScript 检查结果见上文。
- 模型审查提交、embedding 作业均为 0，没有使用真实模型重跑失败合同。

本机完整部署记录、只读核验、测试日志及部署前备份：

`E:\ProofSpaceLegalKG\review-reliability-deploy-20260906`

主要结果：`deployment-verification.json`、`linux-tests.log`、`postgres-concurrency.log`、`public-http-check.json`。备份目录 ACL 仅允许当前用户、SYSTEM 和管理员读取。

尚未完成：真实失败合同重新审查、模型输出质量验收、超出联合窗口的完整综合，以及上文提到的历史法律目录预算问题。旧失败任务会保留旧状态，不会因部署自动变成成功；新的业务复测会产生模型费用，应由用户发起或明确授权。
