# Direct Review 输出契约修复（2026-09-07，r3）

## 本次真实故障

17:12 开始的审查运行 `d4ec8ebf1dd549ab86a634e6e29968dd`，财务域中两批失败；
并非 Token 超限、甲乙方未识别或供应商截断。记录中的四次模型返回均正常停止。

- CF-007：首次用 `check_status`，修复后改成 `status`，但仍只在说明中提及
  Source ID，没有输出无风险判断所需的 `decision_evidence_source_ids`。
- CF-001/002：CF-001 缺少无风险判断依据数组；CF-002 的 Finding 未重复填写
  父检查已经声明的 `check_code`。旧代码先被 CF-001 的引用校验打断，只修它；
  合并后才在结构校验中发现 CF-002 缺字段，唯一一次修复已经耗尽。

此前的 r2 有实现遗漏：统一必填字段表仅在含 CF-005 的分支中设置；新引用要求
与首轮提示、修复结构未一致同步。部分历史测试的 `wire_fixture` 还会自动补充
依据数组，因此仅凭这些测试不能验证真实模型是否按新协议输出。

## 改动

1. 协议 `model-selected-evidence-v4`：首轮、修复均提供同源输出结构。
   所有直接检查明确要求 `check_code/status/decision_note/findings`；当前检查有
   合同 Source、状态为 REVIEWED/NOT_APPLICABLE 且没有 Finding 时，依据数组必填且非空。
2. Finding 的检查归属由程序从父检查绑定。CF 不再要求模型重复字段，FVA 沿用其
   既有可选字段绑定。显式填写了冲突归属仍需修复，不能静默覆盖。
3. 对唯一明确的 `check_status` 别名进行字段名归一化；与 `status` 冲突时拒绝。
   不从说明中的编号猜测模型所选依据，不凭空生成判断或原文。
4. 引用解码后仍收集结构错误；对可独立验证且结构/来源有效的其他检查复用业务
   materialize 校验。各检查的可定位问题一起进入修复提示。未通过的诊断投影不能
   被接受为结果；不可重试业务错误的语义保持不变。
5. 修复仅缩小目标检查与输出要求，保留原批次合同、法律目录。兼容 FVA 压缩行
   格式；移除不属于修复目标的 CF-005 专用输出要求。非目标原始输出仍严格保留。

本次没有更改 PO/ICD/LRE/CCC/MAC 的候选裁决结构；它们已走 Source ID 选择协议。
前端 Finding 展示、法规/规则数据、历史任务状态不变。

## 验证

- 源码与实际打包镜像各运行：**1013 passed, 43 skipped**。
  跳过项是未挂载的历史合同/规则快照；不作为已完成验收。
- 新增 17 个不调用 `wire_fixture` 的原始输出回归场景，覆盖缺失归属、冲突归属、
  状态别名、跨层错误一次修复、修复材料保留及条件必填结构。
- 原始回包重放校验 SHA-256，不补字段：CF-001/002 的首次回包及当时真实修复
  回包现在可形成 1 个有原文绑定的 Finding。CF-007 的两个旧回包依然因缺少明确
  依据选择被拒绝，这是预期，不能宣称旧回包已经合格。
- 主机 JSON Schema 校验器验证 CF/FVA 有/无可用来源共四组结构，检查正常输出
  以及条件必填字段缺失时的拒绝行为。
- 两个失败批次的限量真实模型验证使用冻结合同与法律上下文，最多四次请求，
  禁止供应商自动重试、重复执行与全合同重审；实际两批均 COMPLETED。
  CF-007 一次返回形成 1 个有绑定原文的 Finding；CF-001/002 初次在依据数组中
  夹入 `legal-evidence-catalog-0`，系统正确拒绝，仅重审 CF-002 后通过，形成
  两个有原文依据的无风险检查记录。这个测试证明修复可完成，不证明法律适用
  或业务判断在所有情况下准确。
- 实际计费模型 `dashscope-qwen-plus`：3 次调用（1 次修复），33340 输入 Token、
  1241 输出 Token，合计 34581；Embedding 0 次，没有新建或重跑历史整单任务。
  不同批次/不同上下文下的 Finding 数量不作为准确性验收指标。

命令（工作区根目录，设置 PYTHONUTF8=1）：

```powershell
$env:PROOFSPACE_TEST_REPORT='E:\ProofSpaceLegalKG\review-output-contract-20260907\source'
D:\anaconda\python.exe output/test_multi_role_legal_context.py
$env:PROOFSPACE_TEST_DEPLOYED_IMAGE='1'
$env:PROOFSPACE_TEST_IMAGE='local/proofspace-legal-framework:review-output-contract-20260907-r3'
$env:PROOFSPACE_TEST_REPORT='E:\ProofSpaceLegalKG\review-output-contract-20260907\packaged'
D:\anaconda\python.exe output/test_multi_role_legal_context.py
D:\anaconda\python.exe output/replay_raw_output_contract.py
D:\anaconda\python.exe output/deploy_review_output_contract.py status
```

付费验证脚本 `output/probe_live_output_contract.py` 默认只准备；`--live` 有独占
执行标记，不可重复支付重跑。所有真实原文、回包、备份均在工作区之外的本地目录，
不提交 Git：`E:\ProofSpaceLegalKG\review-output-contract-20260907`。

## 部署与边界

仅替换 13009 Framework/worker；镜像 `review-output-contract-20260907-r3`。
镜像 ID：`sha256:a5fc35f756ce27b465b7c8901033bcfd523207d0447d51e4832b0768e8805465`。
复用 r2 的 109 层，只复制 4 个源码文件；新增 4 层，镜像大小增量 142628 字节。
未改前端、Java、Office、数据库结构及 13005/13007。

新旧任务不会因更新自动重跑；没有将历史失败任务改成成功。本次限量验证不等于
新的七域全合同端到端成功，不证明模型全部业务判断正确，也没有实现尚缺的
跨分片联合判定执行器。此修复消除已确认的输出契约/验证次序矛盾，并保留可靠性
校验；不能承诺未来任意模型输出永不失败。
