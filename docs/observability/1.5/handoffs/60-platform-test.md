# Page 6：测试环境观测基础设施交接

状态：候选实现，尚未部署
分支：`obs/60-platform-test`
工作树：`/home/aituge/worktrees/obs-platform-test`
架构基线：`7cc289e52850de78f0cd985f0cc4dfb2c74ff247`
本轮安全修复起点：`d151c0835d21f40dd444ca56e84e6fab69e0798b`
日期：2026-08-01

## 1. 交付边界

本页只新增测试环境的观测基础设施、应用 Compose 加固 overlay、宿主机日志采集候选方案及隔离/脱敏探针。没有部署、推送或合并，没有修改正在运行的测试容器，也没有读取或接触正式环境。

已核对的测试 Compose 基线：

- Project：`contract-review-code-dev`
- Base Compose source ID：`ours-20260731-713231b1-aff8eb3b-beb880da`
- Base Compose：`/home/aituge/workspace/contract-review-code-dev/releases/ours-20260731-713231b1-aff8eb3b-beb880da/compose.backend.yml`
- Base Compose SHA-256：`22265b0f3c1f8ecb5fa442ca4afa036197ba015aaaf6776ce006cada30571e63`
- Docker Compose：`5.3.1`

本页仅生成候选配置。Page 8 集成前必须再次校验上述基线；基线变化时禁止直接套用 overlay。

## 2. 交付内容

新增目录 `infrastructure/observability-test/`，包含：

- `compose.observability-test.yml`：独立观测栈。
- `compose.application-hardening.test.yml`：与现有测试 Compose 合并的受限 overlay。
- `gateway/Caddyfile`：测试 Caddy 的安全响应头、SSE 和访问日志脱敏候选配置。
- `log-capture/`：不挂 Docker socket 的宿主机日志选择、脱敏及 systemd 候选配置。
- `loki/`、`alloy/`、`tempo/`、`otel-collector/`、`prometheus/`、`alertmanager/`：固定版本配置。
- `grafana/`：数据源、Dashboard provision 和测试 Dashboard。
- `probes/`：Compose、隔离、脱敏、网关、SSE、遥测和镜像供应链验证探针。

### 2.1 Docker trust root

Page 6 的唯一 Docker 安全入口是
`infrastructure/observability-test/docker-trust.sh`。它固定并逐次核验：

- binary：`/usr/bin/docker`；
- context：`default`；
- endpoint/TLS：`unix:///var/run/docker.sock|{}`；
- socket：解析到 `/run/docker.sock`，元数据
  `socket|660|0|984`，GID 984 仍名为 `docker`；
- daemon：`3196b392-cce0-4178-a30a-2a9ff44d271c|afs2600151|/var/lib/docker`。

调用前必须确保 `DOCKER_HOST`、`DOCKER_CONTEXT`、
`DOCKER_TLS_VERIFY`、`DOCKER_CERT_PATH`、`DOCKER_CONFIG` 均未设置，
然后只通过同一文件的入口调用：

```bash
source infrastructure/observability-test/docker-trust.sh
page6_verify_docker_daemon
page6_docker <只读或明确的测试资源操作>
```

`page6_create_container/page6_create_network` 在执行 create 前先把精确
唯一名称和随机 run ID 放入隔离账本。无论 Docker CLI 返回 0 还是非 0，
都会独立按精确名称重新枚举，并核对完整 ID、名称及全部 Page-6 标签：

- CLI 非 0 但 daemon 已创建正确资源时，恢复该完整 ID 并仅按 ID 清理；
- 同名但标签或 run ID 不同属于人工处置红灯，绝不删除；
- 返回 ID 非法、首次核验失败或后续身份漂移时均 fail-closed；
- 只有再次证明该精确名称不存在，才能清除隔离账本。

`page6_validate_*` 在读取或清理前再次核对上述事实；
`page6_cleanup_*` 只接受账本中的完整 64 位 ID，禁止名称清理。进程组清理也必须先通过
`page6_capture_process_identity/page6_verify_process_identity` 核对 owner、
start ticks、PGID、session 和命令行哈希。

Page 7 E2E guard 与 Page 8 集成可以直接 source 此文件并复用上述入口；
如果任一固定字段与服务器事实不符，应 fail-closed 并重新审计，不能改用
环境变量、未知 context 或缓存镜像别名绕过。

## 3. 网络与端口

观测栈使用两个测试专属 `internal: true` 网络：

| 网络 | 接入服务 | 用途 |
| --- | --- | --- |
| `contract-review-code-dev-observability-ingest` | Java、Framework、20 个 Worker、Proof、AI-contract、Smoke、OTel Collector | 应用只向 OTel 写入遥测 |
| `contract-review-code-dev-observability-backend` | Java、OTel Collector、Loki、Alloy、Tempo、Prometheus、Alertmanager、Grafana | Java 管理查询及观测组件内部通信 |

数据库、Redis、OnlyOffice 与 Caddy 不接入上述网络。应用原有网络和本地模型共享网络不变。Java 同时接入 ingest/backend；其他应用只接入 ingest。

观测组件内部端口不映射宿主机。只有 Grafana 暴露 `127.0.0.1:${CONTRACT_REVIEW_DEV_GRAFANA_PORT:-13000}:3000`。测试 Caddy 继续继承 Base Compose 的 `127.0.0.1:13002 -> 80` 与 `127.0.0.1:13082 -> 8082`，本页没有改端口。

## 4. 固定镜像

| 组件 | 不可变镜像契约 | 暴露范围 |
| --- | --- | --- |
| Caddy | `caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` | 继承测试网关 |
| Loki | 必填 `CONTRACT_REVIEW_DEV_LOKI_IMAGE`，仅允许 `grafana/loki:3.7.2@sha256:<批准摘要>` | 内部 |
| Alloy | 必填 `CONTRACT_REVIEW_DEV_ALLOY_IMAGE`，仅允许 `grafana/alloy:v1.18.0@sha256:<批准摘要>` | 内部 |
| Tempo | 必填 `CONTRACT_REVIEW_DEV_TEMPO_IMAGE`，仅允许 `grafana/tempo:2.10.5@sha256:<批准摘要>` | 内部 |
| OTel Collector | `otel/opentelemetry-collector-contrib:0.153.0@sha256:666fb40ee1391aa9f0eddb06ea143ffe215d731ff02cf13ce4c2f44f2a3bf89a` | 内部 |
| Prometheus | `prom/prometheus:v3.13.0@sha256:b96d6068885d3045ae74e149890aa574948f6fcaa6005b82171c6b2be7947998` | 内部 |
| Alertmanager | 必填 `CONTRACT_REVIEW_DEV_ALERTMANAGER_IMAGE`，仅允许 `prom/alertmanager:v0.32.1@sha256:<批准摘要>` | 内部，空接收器 |
| Grafana secret init | 必填 `CONTRACT_REVIEW_DEV_GRAFANA_IMAGE`，仅允许 `grafana/grafana:13.1.0@sha256:<批准摘要>` | 无网络，一次性 |
| Grafana | 与 init 共用同一必填 Grafana 摘要引用 | 仅 loopback |

OTel、Prometheus 和 Caddy 使用服务器上已核验 RepoDigest 的
`tag@sha256` 双绑定；所有服务均为 `pull_policy: never`。Loki、Alloy、
Tempo、Alertmanager 和 Grafana 当前没有已批准的本地摘要，Compose 不提供
可变 tag 默认值，也不记录或伪造摘要。Page 8 只能在批准拉取并验证后，把
五个引用写入测试专属部署 manifest；不得提交、复用正式环境值或使用未知
缓存别名。

`verify-image-supply-chain.sh` 会验证仓库/tag allowlist、64 位小写摘要、
精确引用与 tag 所解析的本地 image ID 相同，以及 RepoDigests 包含相同
`repository@sha256`；任一缺失或不一致均为红灯。部署必须使用
`--pull never`，禁止让 Compose 隐式拉取。

Docker daemon 的 DockerHub 代理当前指向不可用的 `127.0.0.1:7897`。为完成只读原生校验，OTel 镜像从官方 GHCR 内容地址取得并逐层校验：index `sha256:93aad750175cbf1a973ae1c5886c3371f4d800f61be25cdd26870b8441ffe9fa`，linux/amd64 manifest `sha256:388054389612c69d0387ecac256338e4086f6cf072fc8feafb6ce7968dc6946c`。Prometheus 镜像从官方 Quay 内容地址取得并逐层校验：index `sha256:c6b27ea434f8389bfe233fbc7be381cf50587c286e871bc842008f5a1b1908a7`，linux/amd64 manifest `sha256:0e698e35e50d1ddc2d11a4a55b089fe62eb71358a5c204dfafd21bdf8ffe04b8`。中间 OCI、layer、tar 和工具临时目录均已删除，未写入 D 盘。

## 5. 日志采集与留存

Alloy 不挂载 `/var/run/docker.sock`，也不挂载 `/var/lib/docker/containers`。宿主机 systemd 候选服务以 Docker CLI 选择日志，目标容器必须同时满足：

- `com.docker.compose.project=contract-review-code-dev`
- `com.aituge.environment=test`
- `com.aituge.observability.scope=contract-review-code-dev`
- `com.aituge.observability.capture=enabled`
- 完整 64 位容器 ID
- Docker 返回的实际名称仍属于批准的测试前缀
- 非空 Compose service

每次执行 `docker logs --follow` 前都会重新核验 Docker daemon、完整 ID、
实际名称、project、environment、scope、capture opt-in 和 service。只有完整
容器 ID 能作为日志文件名。Follower 进程组仅在 owner、start ticks、PGID、
session 和命令行哈希全部未变化时才会收到信号；如果 leader 已消失或身份
发生变化，脚本会遗忘或拒绝该目标，不会根据旧 PGID 猜测清理，因此不再
声称能安全终止 leader 消失后的孤儿进程。

正式候选输出根精确固定为
`/home/aituge/contract-review-code-dev-test-private/observability-logs/redacted`。
`/home/aituge` 必须是 `aituge:aituge/0750`；独立 private root、
`observability-logs` 和 `redacted` 均必须为 `aituge:aituge/0700`，
且每一级都不是符号链接。现有 workspace 为 `0775`，其 `aituge` 组还包含
其他账号，因此本页没有对 workspace 执行 chmod/chown，也不再把 secret 或
可写日志放在其中。独立 private root 消除了同组账号通过重命名父目录制造
TOCTOU 的边界。

redactor 与安全 launcher 从 `/home` 锚点开始逐级使用
`openat(O_NOFOLLOW)`，持有最终目录 FD；写入、轮转、清理和 manager lock
都基于该 FD，不再重新按路径解析。输出 JSONL 为 mode `0600`，单行输入
上限 256 KiB，输出上限 16 KiB，文本字段上限 4096 字符。无效 JSON、超大
输入或解析异常均写入不含原文的 fail-safe 记录。代码中已删除按路径重新
打开根目录的 `rotate(path)` 接口，运行期轮转只接受已验证且持续持有的 root FD。

安全 launcher 对 manager、follower、redactor 和 cleanup 一律使用
`/usr/bin/python3 -E -s -B`，并在启动前删除 `PYTHONPATH`、`PYTHONHOME`、
`PYTHONSTARTUP`、`PYTHONINSPECT`、`LD_PRELOAD`、`LD_LIBRARY_PATH`、
`BASH_ENV` 和 `ENV`。systemd 候选 unit 通过 `UnsetEnvironment=` 重复收紧
同一边界。恶意 `PYTHONPATH/sitecustomize` canary 已证明不能进入捕获进程，
且不会在候选目录生成 Python cache。

systemd 候选服务不再从工作树或 `current` 符号链接执行。Page 8 必须把经
manifest 核验的内容一次性落到
`/home/aituge/workspace/contract-review-code-dev/releases/observability-page6-v1.5.1/`，
设为 `root:root` 且不可被 service user、group 或 other 写入，并禁止覆盖或
复用这个 release ID。unit 的 `ExecStart` 和 `ReadOnlyPaths` 已固定到该版本；
可变环境文件不能重定向可执行路径。

`/home/aituge/workspace/contract-review-code-dev/observability-capture.env` 必须
是非符号链接普通文件、`root:aituge/0640`，只允许捕获选择器和精确日志根
`/home/aituge/contract-review-code-dev-test-private/observability-logs/redacted`，
禁止放 secret。日志根必须 `aituge:aituge/0700`；
`.capture-manager.lock` 存在时必须是同 owner 的 `0600` 单硬链接普通文件。
unit 仅赋予该精确外部日志根写权限。

每个容器日志 20 MiB 轮转，保留 active + 4 个历史文件；文件及轮转副本保留
10 天。manager 即使空闲也每 5 分钟执行清理。Alloy 只读取 active
`*.jsonl` 并持久化 positions，不读取 `.jsonl.1` 至 `.jsonl.4`。

这不是无遗漏保证：如果 Alloy 停机跨过一次或多次 20 MiB 轮转，宿主机仍
保留的历史文件可能从未进入 Loki。当前没有经验证、同时避免重复的 rotated
file fingerprint/positions 方案，因此此处明确属于 fail-open、best-effort
采集缺口。管理员接口必须通过 `sourceStatus` 将来源标成延迟/不完整，并对
采集 lag 告警；Page 8 必须执行“Alloy 停机跨轮转”的容量和故障演练。未通过
前不得声称 Loki 日志完整。

`docker logs --since 10s` 只能提供 at-least-once 重连，边界记录可能重复。
输出固定携带 `stream=APPLICATION`；本页没有声称 exactly-once，查询侧应在
存在 correlation/event ID 时容忍去重。

## 6. 脱敏与最小化

宿主机 redactor 递归处理 JSON，并覆盖 ANSI、控制字符、JWT、Bearer、常见 API Key、查询参数和键值对。禁止进入 Loki/Tempo/Prometheus 的信息包括但不限于：

- 密钥、Authorization、Cookie、内部 Token 和凭据引用；
- `downloadToken`、`download_token` 及其 query/header/key-value 变体；
- 原始 client/remote/peer IP 与 Forwarded、X-Forwarded-For、X-Real-IP；
- cursor、retryToken、locator、Access Context/Session、highWatermark、PIT、querySnapshot、Idempotency-Key、stream token；
- Prompt、模型响应、请求体、合同正文和原始异常内容；
- tenant/task/run/request/trace 等高基数字段作为 Prometheus label。

Caddy 仅向 stdout 输出 JSON 访问日志：完整 `request.uri`（路径与 query）
直接删除；完整 `request.headers` 映射、`request.host`、`request.method`
以及原始 client/remote 地址也全部删除，而非只删除已知敏感请求头。请求体从未
配置为日志字段。管理员响应增加 `Cache-Control: no-store, private`、
`Referrer-Policy: no-referrer`
和 `Vary: Authorization`。SSE 增加 `no-cache, no-store, private`、
`X-Accel-Buffering: no`、即时 flush 及长连接超时。

宿主机 redactor 只保留登记过的键路径；未知键（包括 ASCII、中文或编码后的
secret-as-key）整体删除。对 level/logger、事件码、provider/model、
operation/status、隐私/路由及 ID 等允许键再执行值白名单，非法值脱敏或整条
记录 fail-closed，不能通过“合法键 + 秘密值”绕过。

OTel Collector 先验证 code-owned service.name、metric name/unit，以及 Span、
Span Event、Datapoint 中允许键的 ID/scope/feature/provider/model/privacy/
route/HTTP/status/operation 值；任何非法 Span、事件、Metric 或 Datapoint
整体 fail-closed 丢弃，再移除 URL/body、鉴权、数据库 statement、异常
message/stack 和生成式 AI 内容。当前只证明安全指标和不含 Link 的 Span
可导出。Loki、Tempo、Prometheus 和宿主机日志保留期均为 10 天。

固定 OTel `0.153.0` 不能通过 OTTL 安全地原位替换 exemplar slice；本页采用
fail-closed 兼容策略：任何带 exemplar 的 metric datapoint 整体丢弃，
不带 exemplar 的安全 datapoint 继续进入 Prometheus。这会损失带 exemplar
的指标数据，但不会把 exemplar 中的 trace/span ID 或属性送出。应用在验证
可安全升级的 Collector 路径前，不得在测试环境指标中附加 exemplar。

固定 OTel `0.153.0` 无法在保留 Span Link trace/span ID 的同时清除 Link
attributes。本页采用 fail-closed：任何包含 Link 的 Span 整体丢弃，并显式
输出红灯 `OTEL_LINK_ATTRIBUTE_SANITIZATION_UNAVAILABLE`。这不代表已支持
Span Link，也不能称为完整异步 Trace。Page 8 在找到并真实验证“保留 Link
身份、删除全部 Link attributes”的固定方案前，必须保持异步 Trace 关闭；
不含 Link 的 Span 脱敏验证与该红灯相互独立。

## 7. Secret 边界

Page 8 必须把 `CONTRACT_REVIEW_DEV_TEST_SECRETS_DIR` 精确设为
`/home/aituge/contract-review-code-dev-test-private/secrets`。从
`/home/aituge` 到该目录的每一级都必须真实、非符号链接；private root 和
secrets 为 `aituge:aituge/0700`。`deepseek_api_key`、
`dashscope_api_key`、`grafana_admin_password` 必须是
`aituge:aituge/0600` 的单硬链接普通文件，可由运行用户读取。验证只能执行
路径、owner/group、mode、类型、link count 与 `test -r`，禁止读取、打印或
计算 secret 哈希。不得退回 group-writable 的测试 workspace。

Framework、20 个 Worker、Proof 和 AI-contract 的既有 secret 目标路径不变，仅把 source 收敛到测试 secret 目录并保持只读。当前目标运行容器使用 UID 0，已只核对目标文件可读性，未读取内容；Page 8 对重建后的目标容器必须复验。

Grafana 不能直接读取宿主机 owner-only 的 0600 文件。因此新增一次性 `grafana-secret-init`：无网络、只读根文件系统、`cap_drop: ALL`，只增加复制所需的 `CHOWN/DAC_OVERRIDE/FOWNER`，将密码无输出复制到专用 Docker volume，设置 owner `472:0`、mode `0400`。Grafana 显式以 `472:0` 运行并只读挂载派生 volume。Page 8 必须验证固定镜像 UID、init 工具、复制/读取路径；密码轮换时必须强制重建 init 服务。

## 8. 探针覆盖

以下是依赖齐全后 Page 8 的目标成功码，不是本轮全部已通过事实。尤其
`ALLOY_FIXED_IMAGE_VALIDATE_OK` 与五个缺失镜像的摘要成功码当前尚未取得：

```text
DOCKER_SAFETY_VALIDATION_OK
CAPTURE_SELF_TEST_OK
CAPTURE_ROOT_FD_SWAP_OK
PYTHON_ENVIRONMENT_ISOLATION_OK
COMPOSE_VALIDATION_OK
ISOLATION_VALIDATION_OK
REDACTION_VALIDATION_OK
GATEWAY_VALIDATION_OK
SSE_VALIDATION_OK
OTEL_FIXED_IMAGE_VALIDATE_OK
OTEL_FIXED_IMAGE_START_OK
OTEL_REAL_METRIC_REDACTION_OK
OTEL_METRIC_VALUE_ALLOWLIST_OK
OTEL_METRIC_STRUCTURE_REDACTION_OK
OTEL_TRACE_VALUE_ALLOWLIST_OK
OTEL_UNLINKED_TRACE_REDACTION_OK
OTEL_LINKED_SPAN_FAIL_CLOSED_OK
IMAGE_REFERENCE_POLICY_NEGATIVE_TEST_OK
CADDY_APPROVED_IMAGE_DIGEST_OK
OTEL_APPROVED_IMAGE_DIGEST_OK
PROMETHEUS_APPROVED_IMAGE_DIGEST_OK
LOKI_APPROVED_IMAGE_DIGEST_OK
ALLOY_APPROVED_IMAGE_DIGEST_OK
TEMPO_APPROVED_IMAGE_DIGEST_OK
ALERTMANAGER_APPROVED_IMAGE_DIGEST_OK
GRAFANA_APPROVED_IMAGE_DIGEST_OK
IMAGE_SUPPLY_CHAIN_VALIDATION_OK
ALLOY_FIXED_IMAGE_VALIDATE_OK  # 仅 Page 8 目标；不是本轮通过项
```

Compose/隔离探针固定 Base Compose 的绝对路径、source ID 和 SHA-256，
并将 Base 与合并后的 config 做字段级差分：服务集合和高风险字段必须不变，
只有明确 allowlist 的 label、logging、Java `LOG_PATH`、观测网络、新 secret
source 和 Caddy source 可以变化。它递归拒绝 formal/prod/production 资源、
Docker socket、Docker 容器目录、未知端口和非测试资源。批准 Base 中已有的
`services.java.environment.PROFILES_ACTIVE=prod` 仅作为精确路径和值的兼容
例外；任何其他位置或资源名称出现正式环境 token 都会失败。

瞬态探针使用随机 run ID、Page-6-only 名称和标签、`internal` 网络及
`network none` 容器，不发布宿主机端口。create 前先登记精确名称/run ID；
无论 CLI 成败，都独立重查该名称。非 0 但已创建的正确资源会恢复完整 ID 并
按 ID 清理；不同身份的同名资源只报人工红灯，绝不删除；只有确认精确名称
不存在才清除隔离账本。读取、日志和清理前继续重验 daemon、ID、精确名称及
全部隔离标签。假 Docker 负测覆盖无资源失败、非 0 但容器/网络已创建、
不同标签同名冲突、非法返回 ID、首次核验失败、身份漂移、人工处置与重试。

日志 E2E smoke 同时创建一个允许目标和一个缺 capture opt-in 的拒绝目标，
确认只采集允许目标、输出 mode 0600/`stream=APPLICATION`、拒绝目标无
文件。网关探针用普通 query、大小写 locator、百分号编码 locator、4096
字符 locator、未知请求头、Host 和独立 method canary，证明完整 URI、整张
请求头映射、Host、Method、body 和原始客户端地址都不进入访问日志；SSE
探针验证不缓冲且无不当压缩。

`verify-telemetry-configs.sh` 使用已双绑定的 OTel 摘要镜像真实执行
`validate` 与启动；Alloy 只有在批准的摘要引用与本地 RepoDigest 一致后才
执行 `fmt --test`。`verify-image-supply-chain.sh` 独立验证固定
repository/tag、摘要格式、本地 image ID/tag 绑定和 RepoDigest。缺少任一
批准镜像时会明确 fail-closed，禁止用未知缓存别名替代。

本页没有对尚未原生启动的 Loki、Alloy、Tempo、Alertmanager、Grafana 作
运行通过声明；这些由 Page 8 在部署前后完成固定镜像原生校验和真实链路
验证。

### 8.1 本轮服务器验收事实（2026-08-01）

Docker trust root 实测通过，daemon identity 为：

```text
3196b392-cce0-4178-a30a-2a9ff44d271c|afs2600151|/var/lib/docker
```

最终服务器套件实测输出：

```text
DOCKER_QUARANTINE_RECOVERY_OK
DOCKER_SAFETY_VALIDATION_OK
CAPTURE_SELF_TEST_OK
CAPTURE_ROOT_FD_SWAP_OK
PYTHON_ENVIRONMENT_ISOLATION_OK
COMPOSE_VALIDATION_OK
ISOLATION_VALIDATION_OK
REDACTION_VALIDATION_OK
GATEWAY_VALIDATION_OK
SSE_VALIDATION_OK
OTEL_FIXED_IMAGE_VALIDATE_OK
OTEL_FIXED_IMAGE_START_OK
OTEL_REAL_METRIC_REDACTION_OK
OTEL_METRIC_VALUE_ALLOWLIST_OK
OTEL_METRIC_STRUCTURE_REDACTION_OK
OTEL_TRACE_VALUE_ALLOWLIST_OK
OTEL_UNLINKED_TRACE_REDACTION_OK
OTEL_LINKED_SPAN_FAIL_CLOSED_OK
IMAGE_REFERENCE_POLICY_NEGATIVE_TEST_OK
CADDY_APPROVED_IMAGE_DIGEST_OK
OTEL_APPROVED_IMAGE_DIGEST_OK
PROMETHEUS_APPROVED_IMAGE_DIGEST_OK
OTEL_LINK_ATTRIBUTE_SANITIZATION_UNAVAILABLE
LOKI_APPROVED_IMAGE_REFERENCE_MISSING
ALLOY_APPROVED_IMAGE_REFERENCE_MISSING
TEMPO_APPROVED_IMAGE_REFERENCE_MISSING
ALERTMANAGER_APPROVED_IMAGE_REFERENCE_MISSING
GRAFANA_APPROVED_IMAGE_REFERENCE_MISSING
```

其中 `OTEL_LINK_ATTRIBUTE_SANITIZATION_UNAVAILABLE` 是功能红灯；五个
`*_APPROVED_IMAGE_REFERENCE_MISSING` 是供应链依赖红灯，均不是通过项。
`OTEL_LINKED_SPAN_FAIL_CLOSED_OK` 只证明包含 Link 的 Span 被整体丢弃，
绝不表示 Span Link 可用或异步 Trace 已接入。Caddy、OTel 和 Prometheus
已经通过精确 `tag@sha256`、本地 image ID/tag 绑定与 RepoDigest 一致性验证；
没有为缺失镜像记录或猜测摘要。

Docker trust 预检后曾尝试官方 `grafana/alloy:v1.18.0`，daemon 原始失败为：

```text
proxyconnect tcp: dial tcp 127.0.0.1:7897: connect: connection refused
```

服务器可一度从 Grafana 官方 GitHub API 列出 v1.18.0 的
`alloy-linux-amd64.zip` 和 `SHA256SUMS`，但官方资产下载的三个有界重试
均在连接 `github.com:443` 时超时。临时目录已删除；未安装二进制，未修改
Docker daemon/代理。Loki、Alloy、Tempo、Alertmanager、Grafana 均仍缺
经批准且本地一致的摘要引用；Alloy 也没有 `ALLOY_FIXED_IMAGE_VALIDATE_OK`。
Page 8 必须解决镜像可达性，按审批结果填入测试专属 manifest，再执行供应链
探针和各组件原生校验；不能把 OTel 通过或静态审查当成其他组件通过。

其余提交前证据：

- Shell/Python/YAML 语法解析通过，`git diff --check` 通过；服务器没有安装
  `shellcheck`，因此没有声称 ShellCheck 通过。
- Approved Base Compose SHA-256 复核仍为
  `22265b0f3c1f8ecb5fa442ca4afa036197ba015aaaf6776ce006cada30571e63`。
- 以 `com.aituge.page6.run-id` 精确过滤，Page 6 瞬态容器和网络均为零；
  `page6-probe.*`、`/tmp/page6-alloy.*`、capture manager/follower 进程均为
  零，Python cache 已删除。
- 改动范围仅限 Page 6 基础设施与本交接文件；UTF-8、NUL、符号链接和常见
  私钥/密钥样式扫描通过。

## 9. Page 8 集成清单

1. 在 `obs/90-integration` 使用最新工作树，并重新核对 Base Compose 路径、source ID、SHA-256、project 和服务集合。
2. 通过批准来源拉取缺失镜像，记录并核验精确 `repository:tag@sha256`、
   本地 tag image ID 和 RepoDigest；把五个批准引用只写入测试专属部署
   manifest，运行 `verify-image-supply-chain.sh`，部署使用 `--pull never`。
   禁止 `latest`、伪造摘要、未知缓存别名或以 YAML 可解析代替原生启动验证。
3. 在 `/home/aituge/contract-review-code-dev-test-private` 创建并验证精确 secret/log 目录链，保持 `aituge:aituge/0700`，不输出 secret 内容或哈希；不得修改现有 `0775` workspace 权限。
4. 原生验证 Loki、Alloy、Tempo、Alertmanager、Grafana 配置、用户、文件权限和启动状态。
5. 验证 Grafana init 以 root 一次性复制、输出文件 `472:0/0400`、Grafana UID 472 可读；轮换后强制重建 init。
6. 使用 Base + application overlay 渲染并再次运行 `verify-isolation.sh`；overlay 绝不能单独运行。
7. 复核每个应用仅接入允许的观测网络，数据库/Redis/OnlyOffice/Caddy 不接入，Java 管理查询只能走 backend。
8. 对重建后的 Framework、20 Workers、Proof、AI-contract 逐一执行 UID 和 secret `test -r` 元数据验证。
9. 由各业务页接入 OTLP 指标和不含 Link 的同步 Span，验证应用到 `otel-collector:4317/4318`，并确认敏感字段没有进入 Span 或指标 label。异步 Trace 必须保持关闭，直到固定方案真实证明保留 Link trace/span ID 且删除全部 Link attributes。
10. 安装/启用宿主机 capture systemd 候选服务，验证 `-E -s -B`、环境变量
    清除、`UnsetEnvironment=`、外部 private 目录链、root FD 锁定、权限、轮转、
    10 天清理及进程 owner/start/PGID/session/命令哈希保护；不得声称可按已
    消失 leader 的旧 PGID 安全清理。
11. 完成真实 DNS、健康、查询、Dashboard、告警、不含 Link 的同步 Span、
    指标、日志、时间线与 SSE 验证；执行“Alloy 停机跨轮转”容量/故障演练，
    验证 lag 告警和管理员 `sourceStatus` 延迟/不完整状态；不得把 linked-span
    丢弃测试包装成完整 Trace 通过，也不得声称 Loki 采集无遗漏。
12. 执行 sentinel 泄漏测试、负面隔离测试和回滚演练；观测组件故障不得破坏业务主链路。
13. 将最终候选按 manifest 一次性发布为 `observability-page6-v1.5.1`；
    验证 release 非符号链接、`root:root` 且 service user/group/other 不可写，
    并验证 env 为 `root:aituge/0640`、private/log/secrets 目录链为
    `aituge:aituge/0700`、secret 与锁文件为 `aituge:aituge/0600` 单硬链接
    普通文件；任一不符均禁止启动。

建议环境变量：

```text
CONTRACT_REVIEW_DEV_PLATFORM_SOURCE_DIR=/home/aituge/workspace/contract-review-code-dev/releases/observability-page6-v1.5.1/infrastructure/observability-test
CONTRACT_REVIEW_DEV_BASE_COMPOSE=/home/aituge/workspace/contract-review-code-dev/releases/ours-20260731-713231b1-aff8eb3b-beb880da/compose.backend.yml
CONTRACT_REVIEW_DEV_BASE_COMPOSE_SOURCE_ID=ours-20260731-713231b1-aff8eb3b-beb880da
CONTRACT_REVIEW_DEV_OBSERVABILITY_LOG_ROOT=/home/aituge/contract-review-code-dev-test-private/observability-logs/redacted
CONTRACT_REVIEW_DEV_TEST_SECRETS_DIR=/home/aituge/contract-review-code-dev-test-private/secrets
CONTRACT_REVIEW_DEV_LOKI_IMAGE=grafana/loki:3.7.2@sha256:<批准的测试摘要>
CONTRACT_REVIEW_DEV_ALLOY_IMAGE=grafana/alloy:v1.18.0@sha256:<批准的测试摘要>
CONTRACT_REVIEW_DEV_TEMPO_IMAGE=grafana/tempo:2.10.5@sha256:<批准的测试摘要>
CONTRACT_REVIEW_DEV_ALERTMANAGER_IMAGE=prom/alertmanager:v0.32.1@sha256:<批准的测试摘要>
CONTRACT_REVIEW_DEV_GRAFANA_IMAGE=grafana/grafana:13.1.0@sha256:<批准的测试摘要>
CONTRACT_REVIEW_DEV_GRAFANA_ADMIN_USER=<非默认用户名>
CONTRACT_REVIEW_DEV_GRAFANA_PORT=13000
```

上面的 `<批准的测试摘要>` 是交接占位符，不是可用值，禁止直接复制部署。

`CONTRACT_REVIEW_DEV_PLATFORM_SOURCE_DIR` 只供 Compose bind source 使用；capture
unit 的可执行路径已经固定，环境文件不能覆盖它。

## 10. 回滚边界

Page 8 必须保存部署前容器/网络/volume 清单和 Compose config。回滚只允许删除 `contract-review-code-dev-observability-*` 测试资源以及明确新增的测试连接、systemd unit 和派生 Grafana secret volume；禁止全局 prune，禁止停止或删除不在清单内的容器。

脱敏日志、positions 和测试观测存储必须按批准的 10 天留存与清理策略处理，不能因回滚直接删除取证数据。任何凭据复制、权限修复、数据回放或清理动作都应留存安全审计证据。