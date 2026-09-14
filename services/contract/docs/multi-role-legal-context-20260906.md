# 多业务角色、法条正文与风险详情引用

## 本次范围

仅修改 `feature/legal-knowledge-graph-e2e` 的本地开发验证环境 13009。不修改 13005、13007、历史审查数据、原始法规包、规则发布状态或七域调度方式。未提交/推送。

1. AI 已确认的 `business_roles[]` 从 party artifact 传到规则选择器、模型提示词及 `RuleReviewResult`。兼容旧 `business_role`；一方可以有多个角色。角色不替代用户选择的甲乙方立场。任意匹配角色可使规则进入候选，同一规则 ID 只保留一次。租户、合同类型、强弱标准、日期、法域和规则状态过滤不变。
2. 商务财务、通用基础域（含候选裁决）及横向审查改用 `review_legal_evidence_catalog`。发送本检查绑定的检索单元完整正文并去重；不再使用旧 7000 预算省略正文，也不截掉条末例外。这里的完整正文指检索单元，不是整部法规。现有供应商上下文限制仍存在；不通过空目录隐藏超限。
3. 提示词要求在 `issue` / `decision_summary` 自然解释实际适用的“法规名称＋条号＋与风险的关系”。模板生成 Finding 时保留模型已有的法律分析。仅给模型真正收到、与本检查绑定且说明中确实引用的法条建立 Evidence ID，不再给没有使用法律的旧说明自动挂候选法条。

前端 `ContractRiskIssue` 仅把说明中已有的匹配法条名称变成行内按钮。点击显示本次结果保存的冻结正文，不请求法规库最新版本。同名条号对应多个版本时不任意选择。风险详情不新增合同原文或业务规则列表，也不恢复已移除的规则侧栏。

效力/适用性未知的法条继续明确标注待核验，不包装为已确认的现行依据。本次不是法规元数据修复或法规质量验收。

## 缓存与兼容

- 规则执行缓存包含完整角色数组，reviewer version 更新为 `rule-review-v2-multi-role`；不会复用旧的单角色丢失结果。
- AI 主体识别缓存无需清空：其中已经保存了正确的多角色数据。
- ai-contract 同步结果 schema 的可选 `business_roles` 字段；Java 保存原始规则结果 JSON，无需迁移或重建。
- 旧任务不重算、不改写；重新审查才会产生新版模型说明。
- 现有规则审查仍是独立 PREVIEW 结果路径；本次没有把所有候选规则强行转换为七域 Finding，也没有提高规则模型调用预算。

## 无付费模型回放

真实冻结输入：`review-e3f557f2be9940fa98d450684d391779`。

- AI 角色：供货方、出卖方。
- 本机规则快照：34,959 条。
- 同一强势采购合同选择条件：旧的角色丢失结果为 0 条候选；保留多角色后为 40 条候选（本快照中对应出卖方）。财务检查进一步检索得到 1 条规则，不以候选总量作为最终审查数量。
- 原财务提示词状态 `OMITTED_TOKEN_BUDGET` → `INCLUDED`；2 个法条检索单元、430 字符正文逐字一致。
- 回放 Docker 使用 `--network none`，没有调用 AI、embedding 或提交新审查。

## 测试与部署

主工作目录 `output/test_multi_role_legal_context.py`：Linux 断网容器回归 **464 passed, 43 skipped**。跳过项需要未挂载的旧合同固定样本或规则快照；不计为通过。覆盖多角色联合筛选/去重/缓存、旧调用兼容、排除对方规则、全文不截断、横向与通用提示词、实际 Finding 引用绑定，以及此前审查可靠性回归。

前端 `node node_modules/vitest/vitest.mjs run src/features/contracts`：**23 files / 171 passed**；相关文件 ESLint、TypeScript 与生产构建通过。

镜像标签统一为 `multi-role-legal-context-20260906`，通过已有依赖镜像增加小型代码层；没有重新构建模型、OCR 或大型依赖。部署只涉及 framework、framework-worker、ai-contract、frontend。执行器 `output/deploy_multi_role_legal_context.py` 在更新前检查无活动任务、备份本地审查表，更新后校验容器源码 SHA-256、原网络和挂载不变及其他受保护容器身份不变。

完整本机记录位于 `E:/ProofSpaceLegalKG/multi-role-legal-context-20260906/`：`linux-tests.log`、`frozen-replay.json`、`deployment-verification.json`。该目录 ACL 限当前用户、Administrators、SYSTEM，不提交源码库。

验收边界：本次没有新发起付费模型端到端审查。前端测试验证行内引用渲染及冻结正文组件，未将其描述为已登录浏览器下的新合同点击验收。
