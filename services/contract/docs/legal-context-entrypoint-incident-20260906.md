# 13009 法条请求入口 NameError 回归

## 故障证据

- Java task：`887119968380915795`；用户截图追踪号：`1248783450299715584`。
- Framework run：`3bab0645b76448e5bf33dc2a3ba491e7`。
- Review：`review-9c08c25cd2384559b97b3bb36d7244c3`。
- 2026-09-06 23:38:02 开始，23:39:33 失败（本机 Asia/Shanghai）。
- `parse_contract`、`resolve_parties`、`extract_contract_ir` 均成功；`finalize_review` 失败。
- 存储的准确错误：`Direct risk review failed: name 'compact_legal_evidence_catalog' is not defined`。

这是上一轮法条正文接入改动引入的代码回归，不是本次模型输出不稳定或 Token 超限。替换三个审查模块的旧 helper 导入时，更新了提示词组装逻辑，但遗漏了 `commercial_request_from_context` 与 `generic_request_from_context` 中用于计算法条文本输入量的两处调用。该调用在空法条列表时也会执行，故不必命中法规即可触发 NameError。Java 的 `FRAMEWORK_RUN_FAILED` 是通用对外错误码，不能据此判断是模型服务故障；95% 只是当前阶段进度。

## 修复与测试补缺

两处工厂调用统一改为 `review_legal_evidence_catalog`，不恢复旧的裁剪/省略逻辑、不关闭证据验证、不改失败记录状态。

旧回归主要直接构造 ReviewRequest 测试 prompt/reviewer，固定合同样本的入口测试又因外部样本未挂载而跳过，因此此前的通过数量未覆盖这两条真实入口。

新增无需外部合同文件的 `services/contract/tests/test_legal_context_entrypoints.py`：

- 五个基础域 × 有/无法条 × model/dict 传输，共 20 项，实际执行 `RiskReviewPlanBuilder -> request_from_context -> prompt`。
- 使用正式动态分批的 absence-only 配置，不放松原证据目录校验。
- 三个审查模块禁止加载已退役 helper 的 AST 回归，共 3 项。
- 修复前新增测试复现相同 NameError，修复后 23 项通过。

断网 Docker 全套相关回归：**487 passed, 43 skipped**。跳过项仍为旧固定样本/挂载条件缺失，不计为通过。

## 真实失败输入回放

`output/replay_failed_review_entrypoints.py` 只读本机已保存的 IR、183 个文本块、主体信息及冻结法律证据，然后通过 `--network none` 容器回放。

- 五个基础域，八个请求入口全部成功。
- 商务财务 4 批，其他四域各 1 批；法条目录和原检索单元正文逐字一致。
- 新模型请求 0，新 embedding 请求 0，没有重新上传/识别，没有生成虚构的最终审查结果。
- 这是入口与提示词回放，不是重新完成一轮付费端到端审查。原失败任务此前已发生的 IR 模型调用不属于此次回放。

## 本机部署

仅更新 13009 `framework`、`framework-worker`，新镜像 `local/proofspace-legal-framework:legal-context-entrypoint-fix-20260906`。基于已有镜像增加两个 Python 文件的小代码层，无需重建模型/OCR/前端镜像。

部署前检查无活动审查任务、保存本地备份。部署后核对文件 SHA-256、服务健康和重启次数、历史任务不变、所有非目标容器身份不变。

日志及验证文件：`E:/ProofSpaceLegalKG/legal-context-entrypoint-fix-20260906/`。未创建/推送源码分支或提交，未提交合同数据和密钥。历史失败任务保留原状；本次未自动触发付费重试。
