# 模型选择实际证据，程序保留原文绑定

2026-09-07；分支 `feature/legal-knowledge-graph-e2e`，基于 `7d50baa` 的现有未提交工作继续修改。本次未提交、未推送。

## 结果与范围

已部署至本机 13009 的 framework、framework-worker。前端、Java、ai-contract、数据库、13005、13007 均未重建或修改。

影响三个使用 CandidateDecision 的审查域：履约义务、知识产权/保密/数据、责任/救济/退出。财务及主体审查原有直接输出路径、七域编排、法律和规则库接入保持原状。

原有 Finding 字段、法条引用、合同原文定位、编辑器操作接口不变。没有新增前端主证据/辅助证据/反证面板。

## 原因与新链路

此前将触发审查的事实及其原文绑定永久视为 Primary，模型没有 Primary 输出字段。实际失败记录中，输入 26 条 Primary 原样出现在 Supporting，输入 10 条可选 Supporting 原样出现在 Counter，修复后仍重复。此记录证明字段搬运，不证明模型内部的推理过程。

新链路：

1. 原有候选生成、检查范围和事实→原文位置映射保持冻结，历史类中的 `primary_evidence_source_ids` 名称保留兼容；对模型明确展示为 `trigger_material_source_ids`，不代表已经证明结论。
2. 每个候选的完整材料并集作为 `material_source_ids`。程序不再用旧的召回角色列表限定某段正文只能作为辅助或反证。材料仍不得跨候选、检查或文档版本边界；ABSENCE 不能证明一个机制已经存在。
3. 输入候选和原文目录改为具名对象，不再依赖候选压缩数组及其独立图例。原文完整保留，同时给出绑定的结构化事实；目录 ID 只在键中出现一次。
4. 模型必须明确输出 `primary_evidence_source_ids`，说明本次裁决实际依据；Supporting 补充依据，Counter 反驳或缓释风险。NO_RISK 的 Primary 可以与 Counter 重叠。是否相关仍是需要模型正确完成的语义判断，ID 在材料池中不等于结论正确。
5. 缺失、错误类型、重复、未知或越界的 Primary 不会自动补成所有触发材料。沿用最多一次修复；修复输入包含完整具名材料和原文目录，并允许重新选择 Primary，不再形成“要求补字段却禁止改字段”的冲突。
6. 创建独立的已选证据投影，不修改原始候选。最终裁决、风险合并、原文引用和严重程度计算使用已选材料。保留合并时核心/上下文的区别，避免公共背景导致两个独立风险被合并。字面期限因素从已选核心证据重新计算。
7. 缺失机制风险必须明确引用其已核验范围的 ABSENCE；未知情况允许 INSUFFICIENT_EVIDENCE 和空 Primary，并保留未完成状态，不制造成功结果。

## 测试

执行入口（仓库父目录）：`output/test_multi_role_legal_context.py`。实际 Docker/pytest 命令及完整输出保存在私有报告目录的 `linux-tests.json`、`linux-tests.log`。

环境：Docker `--network none`，源码只读挂载，fake completion，不调用模型或 embedding。

最终结果：**926 passed, 43 skipped, 4 warnings，23.41 秒**。43 项跳过因为旧固定样本目录/旧 attempt artifact 或规则快照未挂载，不代表通过。

新增 `tests/test_model_selected_evidence.py` 的 19 项测试覆盖：

- 26 条材料完整进入具名提示，Primary 是明确必填的模型输出字段。
- 明确选择 1、2、26 条时，裁决、根风险、Finding 和原文 anchor/hash 都只包含选中证据。
- 冻结候选不被修改；旧辅助材料允许成为实际依据；NO_RISK 同一段原文可同时为 Primary/Counter。
- 旧错位响应不能自动继承 Primary；修复能改此字段并有完整原文；同样错误重复返回时不无限重试。
- 非数组、null、非字符串元素和空依据的修复路径。
- 未知、跨检查、重复编号仍拦截；ABSENCE 不可成为 Counter；未引用材料不能暗中提供严重程度证据。
- 不足以裁决时保留空依据和部分未完成状态。

旧回归最初发现的两项问题（独立风险误合并、字面期限因素丢失）均已在生产逻辑修复，没有删除这些测试。

## 真实冻结数据的离线回放

使用失败 run `2995dc042a2c49a39969c4c7fe754a6f` 的原始 IR、原文和两次 raw response，在构建后的镜像中离线执行 `output/replay_model_selected_evidence.py`。

- 9 个基础批次的真实 request/prompt 构造全部通过，包含此前估算 6060 的财务批次。
- 验收候选保留 26 条触发绑定，完整材料池 36 条；所有原文逐字保持。
- 两个旧错位响应均因没有明确选择 Primary 被识别，不补造字段。
- 人工构造的 2 条引用响应用于测试字段和原文绑定，映射通过；未知和跨检查编号被拦截。
- **上述回放不是新的真实模型裁决，不能证明原 NO_RISK 判断正确，不能当作新合同审查成功。未修改原失败任务。**

该履约批次具名提示为 34,997 字符（此前 25,714）；增加的是字段说明和结构化事实，并未新增常规模型调用次数。字符不是 token，未来实际耗时、token 和模型选择质量还需真实审查验证。

## 部署和验证

镜像：`local/proofspace-legal-framework:model-selected-evidence-20260907`

镜像 ID：`sha256:238cb9001245710e73bdfd70bb781a5825df4e092d73a01a7887fcd41f51c547`

基于已运行的 payload-limits 镜像，仅 COPY 一个生产文件；构建上下文约 510 KB，基础层复用，没有重新构建全部大型依赖。

Compose：`aituge-deployment/environments/legal-evidence-e2e/rules-preview.override.yml`，只替换 framework/worker 两个镜像引用。

部署前通过 Compose 隔离检查和“无活动审查”检查，保存任务备份。部署后：

- framework healthy，worker running，重启计数均为 0，文件 SHA-256 与本机修改一致。
- 两个服务的挂载及网络保持不变；其余受保护容器的 ID、镜像、挂载、网络保持不变。
- Java 审查任务及 Framework run 的数量、状态哈希保持不变。
- 13005、13007、13009 首页均 HTTP 200；vettingtest 通过 curl GET 200 且匹配原前端 build ID。Python urllib 公网探测仍返回 403，记录为客户端差异，不掩盖。
- 本次模型调用 0，embedding 调用 0，没有登录浏览器后提交新合同测试。

私有报告目录：`E:/ProofSpaceLegalKG/model-selected-evidence-20260907`。包含部署前备份、测试命令和日志、冻结回放、部署验证；ACL 限当前用户、Administrators、SYSTEM。原始合同和数据库备份没有写入 Git。

下一步：通过原前端重新发起合同审查，观察真实模型是否正确选择及解释证据。旧失败记录和已完成历史不会被此部署重写，也不宣称模型以后不会出现其他格式或判断错误。
