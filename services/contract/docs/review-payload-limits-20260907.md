# 审查长度上限清理与证据编号错位定位（2026-09-07）

## 本次故障的可验证事实

追踪号 `1248789926556749824`；Framework run `2995dc042a2c49a39969c4c7fe754a6f`。

两个独立失败：

1. CF-005 的估算值 6060 被 `CommercialReviewRequest.estimated_input_tokens <= 6000` 拒绝，尚未调用模型；不是供应商拒绝上下文。
2. PO-004 / `ACCEPTANCE_MECHANISM_REVIEW` 返回 NO_RISK，但 Counter Evidence 选错。

冻结输入和两次原始响应逐项比较：

| 输入的候选字段 | 数量 | 模型实际填写位置 |
| --- | ---: | --- |
| primary_evidence_source_ids | 26 | 原顺序逐项复制到 supporting_evidence_source_ids |
| allowed_supporting_evidence_source_ids | 10 | 原顺序逐项复制到 counter_evidence_source_ids |
| allowed_counter_evidence_source_ids | 15 | 没有按这份允许列表选择反证 |

候选输入使用压缩的数组行，三个字段连续排列，字段名放在独立 legend；输出则是具名对象。以上是**可以验证的整列错位复制**，不是对模型内部思考的推测。26+10 个唯一编号覆盖当前检查的 36 条文本目录。10 条反证中 7 条不属于当前候选的合法 Counter 列表，内容包括工期顺延、风险转移、保证金等上下文碎片。还有同文不同 ID 的情况，不能仅凭文字相同直接解除证据身份约束。

第二次修复的整个该 CandidateDecision 与第一次完全相同。代码已经提供反证允许字典与原文目录；并不是本次修复把原文目录裁掉了。正文中的“验收机制完整”与结构化编号清单是两个不同输出字段；这次错误不是在说明句中拼接了随机编号。

本次只定位该选证问题，**没有把编号错位当作已修复**，没有扩宽 Counter 白名单、伪造审查成功或重新提交收费任务。后续应把压缩候选行改为字段就地命名的对象，并对选证修复进行针对性回放；是否有充分依据判 NO_RISK 仍需按真实原文判断。

## 已清理的限制

- 商务和通用域请求残留的 6000 估算 Token 上限。
- 商务、通用、候选审查在收到完整结果后仍以 4000 completion tokens 判失败的重复拦截；横向候选 5000 估算值拒绝。
- 输入、IR 提取、七域候选、风险说明、规则与法条证据、结果协议、汇总统计共 284 个文本或动态列表字段上限。逐字段清单及防回归用例见 `tests/fixtures/review_payload_limit_inventory.json` 和 `test_review_payload_limits.py`。
- 检查记录裁切、汇总时截取标题/问题/原文，以及只取前 8 条证据等有损截断。
- 单条规则过长直接跳过、达到固定调用数丢弃剩余规则；现按完整单条规则打包，每个批次最多执行一次，目标超额只记诊断。
- 完整单项检查不再因估算超额拆散关联证据或报“原子组件过大”；独立检查仍可无损分批。
- 旧法条 compact 入口保留函数签名以兼容旧调用，但不再删正文或省略整份目录。
- 审查调用显式启用 `use_provider_output_default=True`，不发送应用级 `max_tokens`，也不回落注册表 8000 默认值。未启用该参数的其他模型调用保持原行为。
- MySQL 审查结果中的标题、主体快照、缺失证据范围与说明共 9 个列扩为 LONGTEXT，防止模型结果通过后再因短 VARCHAR 入库失败。版本化 SQL 位于 Java 仓库 `contract_review_payload_text.sql`。旧版本代码可读取扩容后的列；不自动缩列回滚，以免截断新数据。

仍保留：证据身份和原文绑定、哈希、租户权限、合法枚举/编号格式、固定七域协议形状、空值/必要字段语义、有限重试与超时/并发、数据库与供应商的真实容量。检索规划器的时间/深度/防循环预算，以及分页和人工操作 API 的防滥用限制不是此次审查载荷拒绝阈值。成本指标和无损分批目标仍然记录，不据此丢文本或判整单失败。

## 验证范围

- 离线 Linux 容器 `--network none`：**907 passed, 43 skipped**；跳过项需要未配置的旧真实 fixture / 旧批次产物 / 规则快照，不冒充通过。
- 覆盖 6060 / 24001 / 100001 输入估算的五个真实请求工厂；9001 completion tokens；长说明/建议；超过 8 条汇总证据；规则长原文；旧法条入口零预算仍保留全文。
- 用 fake transport 验证审查不发送 max_tokens，非审查调用仍使用已有配置；没有真实模型或 embedding 调用。
- 已保存的真实失败 IR 在构建镜像内离线重放：9 个基础批次全部能构造请求和提示词，CF-005 估算 6060 的批次通过。不是重新生成模型判断，不能据此宣称原合同整单审查已成功。
- 在 MySQL 连接级临时表先演练扩列及 20001 字符回读，再对独立 13009 数据库实施。逐表原有行摘要前后相同，备份保存在受限本地目录。

## 本地交付

工作分支仍为 `feature/legal-knowledge-graph-e2e`；没有提交或推送。

镜像：`local/proofspace-legal-framework:review-payload-limits-20260907`（Framework / worker 共用）；`local/proofspace-legal-contract:review-payload-limits-20260907-r2`。

部署期间首版 ai-contract 增量镜像漏带 `contract.risk.review_ledger`，发生启动失败；停止故障容器后补齐该依赖，构建 r2。r2 已通过离线 `contract.api.app` 启动模块导入和冻结真实合同的计划构建检查。这是打包遗漏，不作为测试成功隐去；最终以部署验证报告中的健康状态为准。

最终部署验证通过：Framework 与 ai-contract healthy，worker running，三者重启计数均为 0；52 份运行文件哈希与工作区对应源码一致（Framework/worker 各 20、ai-contract 12）。13005、13007、13009 的本机 GET 均为 200；vettingtest 公网 curl GET 为 200 且前端 build ID 与运行镜像相同。Python urllib 的公网探测返回 403，已与成功的 curl GET 分开记录，未把客户端差异伪装成所有探测均成功。受保护容器身份/挂载/网络和审查任务记录摘要前后不变。

Framework 镜像 ID：`sha256:d30487c45979d05ca94dc347730626d75b977977f72bf8bb166d7d10257d0b35`；ai-contract r2：`sha256:dda66fc79c35f5b4a44cee81f04cb30dc9f1f028dddf6f137ea355d58c8770ec`。

本地私有报告：`E:/ProofSpaceLegalKG/review-limit-audit-20260907/`。原始模型响应、合同 IR、数据库备份不进入 Git。部署仅更新 13009 Framework、worker、ai-contract；不重建前端、Java、OCR、13005 或 13007。

启动器：工作区 `output/deploy_review_payload_limits.py`；测试命令：设置 `PROOFSPACE_TEST_REPORT=E:/ProofSpaceLegalKG/review-limit-audit-20260907` 后运行 `D:/anaconda/python.exe output/test_multi_role_legal_context.py`。模型产出不再受这些应用级配额限制，后续审查可能增加输出长度/Token；真实模型服务的上下文容量、默认输出容量和错误仍需显式处理，不能声称无限容量。
