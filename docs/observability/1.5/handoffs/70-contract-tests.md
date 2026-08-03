# Page 7 Python checkpoint：最终验证报告

日期：2026-08-02

最终状态为 `passed-with-frozen-contract-xfails`。Page 7 权威验证中的契约、DDL 与宿主机安全门禁均已通过；两项冻结 OpenAPI 契约缺陷继续保持红灯，不能计入全绿。

## 范围与边界

- 分支：`obs/70-contract-tests`；Runner HEAD：`c60d5823a9c1362d4a270c68409a274b97b03a8b`；当前工作更改尚未提交。
- 冻结基线：`7cc289e52850de78f0cd985f0cc4dfb2c74ff247`。
- 未修改冻结 README/01—07、OpenAPI、业务代码、DDL、配置、部署或 secrets。
- 未执行 push、merge 或 deploy；未修改正式链路；真实 E2E 未运行。
- Page 8 尚未启动，本 checkpoint 不声称已经生成或验收 integration manifest。

## 最终权威运行

2026-08-02 完成的最终权威 Python 套件结果如下：

- 真实退出码：`0`；strict 状态：`passed`。
- Ruff 0.12.7：`check` 通过；`format --check` 返回 `9 files already formatted`。
- pytest：`55 passed, 2 strict xfailed, 0 failed`。两项 xfail 按 strict 预期失败执行，未被跳过。
- 3 项 implementation DDL 验证全部通过。
- 6 项宿主机安全测试逐项退出 `0`。
- 聚合标记：`OBS_CONTRACT_HOST_SAFETY_MATRIX_OK cases=6 real_e2e=not-run`。
- 套件标记：`OBS_CONTRACT_SUITE_OK capability_manifest=validated host_safety=validated`。
- `real_e2e=not-run`：本轮没有启动或修改业务 Docker 容器，也没有声称真实 E2E 或生产验证。

## Implementation pin 与私有快照

- Page 1：`ce65c64986d24d17e49f1381e38debcbf6ee4f74`
- Page 2：`0e6f00d1983130ced6f0158c50637b741bcdc678`
- Page 3：`6216b324bdd5d3da6a754ed466567853dbfa6fed`

Runner 对三个实现 pin 分别核验 branch、origin、冻结基线祖先关系、HEAD、clean、实际消费文件的 Git blob 与 SHA。执行输入来自已批准的 Git blob 私有快照，不读取 implementation 工作树中的未提交字节。快照根目录权限为 `0700`，文件与 manifest 权限为 `0600`，并以只读方式挂载。

## 六项宿主机安全门禁

以下标记分别出现一次且对应测试均退出 `0`：

1. `OBS_CONTRACT_SOURCE_SAFETY_OK`
2. `OBS_RUNNER_RESOURCE_SAFETY_OK`
3. `OBS_E2E_GUARD_SAFETY_OK`
4. `OBS_E2E_PROBE_SAFETY_OK`
5. `OBS_E2E_SECRET_SAFETY_OK`
6. `OBS_CAPABILITY_SCAN_SELF_TEST_OK`

门禁覆盖 `/usr/bin/bash -p`、固定 `PATH=/usr/bin:/bin`、受信工具 SHA/owner/mode 校验、hostile PATH、恶意 `BASH_ENV`/`ENV`、资源归属与清理、fd/no-follow、秘密文件权限和 capability 扫描器自测。

Page 6 信任锚点保持为：

- commit：`fbce7a11dec66cd7957b6831d0dc104ad4758985`
- `docker-trust.sh` SHA-256：`15f0d75567c3ff4da4725507ad5e689eeb766da008a6602766386b13006b531f`

## 两项 strict xfail：已执行但保持红灯

两项均归属于冻结 OpenAPI 04/06，记录为 `executed=true`、`claim=red`、`countAsGreen=false`：

- `test_masked_source_ip_schema_accepts_hmac_sha256`：`hmac-sha256:` 加 64 位摘要的实际长度为 `76`，实现接受该值，但冻结契约上限仍为 `64`。
- `test_event_detail_mapping_has_exact_internal_lookup`：外部事件详情允许 `TASK_EVENT/MODEL_EVENT`，但内部契约缺少对应的按 `eventId` 精确查询 Path。

它们不是 Page 7 实现失败，也不是可计入通过数的绿色结果；pytest 通过 strict expected-failure 语义确认缺陷仍然存在。

## 七次执行历史

以下历史完整保留；前六次均为失败、证据不可用或已失效尝试，`countAsGreen=false` 且 `countAsFinal=false`：

1. `upstream-pin-invalidated / interrupted-before-tests`：Page 3 旧 pin `1af8937d70fb571240201bc3a52ebd40e411b668` 被替换为 `6216b324bdd5d3da6a754ed466567853dbfa6fed`，在 pytest 前中断。
2. 第一次真实 pytest：`11 failed, 48 passed, 2 xfailed`，不是最终通过。
3. 一次完整运行的 stdout 与退出码丢失，分类为 `evidence-unavailable / not-counted`，不能推断结果。
4. Ruff format gate 失败，涉及 2 个文件；对应证据 SHA-256 为 `692b67d7dd711163c4b1588123e1d7f52664319d12161160c077ac8429117439`。
5. pytest：`1 failed, 54 passed, 2 xfailed`；失败原因是已过时的 mode 断言；证据 SHA-256 为 `6f0728f0955ae210fe7f02a0986a8e970b1ccb8d4c1e7ed5ecbccfee3479d8c1`。
6. pytest：`1 failed, 54 passed, 2 xfailed`；失败原因是已过时的 curl 断言；证据 SHA-256 为 `d77d12a5d60bd157afb99a6834c9ad4ca8a8eab3f35d7b34ea80e34418e9732a`。
7. 最终权威运行：`55 passed, 2 strict xfailed, 0 failed`，退出码 `0`。该次可作为 strict 套件通过证据，但两项冻结契约 xfail 仍为红灯，不能声称全绿。

## 最终证据

- 原始证据大小：`4004` bytes
- 原始证据权限：`0600`
- SHA-256：`6c587cbcb32a83f786508e9ef3e95584dd602c1b45a36f7bb2b7abd5ce4007ee`
- root 已核验内容、SHA、owner 与 mode；核验完成后已删除临时证据文件。

8 个冻结合同文件的 SHA 校验全部通过；记录的 02 合同集合 SHA-256 为：

`db78612a61c1f4a4f7085cebeea9c20e7bae1075d4014f4fa3c090df840dfd99`

## 明确未声称

- 未修复或覆盖冻结 OpenAPI 缺陷；两项 strict xfail 继续保持红灯。
- 未运行真实 E2E，也未进行生产验证。
- 未启动 Page 8，未声称存在已验收的 integration manifest。
- 未 push、merge、deploy 或创建 commit；Runner HEAD 之后的更改仍在工作树中。
