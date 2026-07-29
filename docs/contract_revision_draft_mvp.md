# 合同风险修订草案 MVP

该能力是现有合同风险审查完成后的独立、附加能力。它不修改风险结果接口、正式
Finding/Evidence DTO、正式 Result Hash、Result Sink 或风险审查执行状态。

## 内部接口

```http
GET /v1/internal/contract-reviews/{review_id}/revision-drafts
    ?generation_id={generation_id}
    &result_hash={result_hash}
```

内部任务侧也可先调用最小生成入口：

```http
POST /v1/internal/contract-reviews/{review_id}/revision-drafts:generate
Content-Type: application/json

{完成结果的只读投影、generation_id及同代Contract IR}
```

POST只向Revision Draft独立Source Provider注册不可变输入并生成缓存，不写正式结果。
前端仍只调用GET，不需要提交Finding或IR。

接口由 `services.contract.capabilities.revision_draft_api` 提供，默认不进入公开
OpenAPI。部署时通过只读的完成结果 Provider 注入正式风险结果及其同代 Contract IR。
当前独立运行方式可配置：

```text
CONTRACT_REVISION_RESULT_FILE
CONTRACT_REVISION_IR_FILE
CONTRACT_REVISION_GENERATION_ID
MODEL_PACK_ID
CONTRACT_TEST_TENANT_ID
CONTRACT_REVISION_DRAFT_CACHE_DIR
```

如果前端只能访问 Java，建议 Java 新增同路径透传接口，并原样传递
`review_id/generation_id/result_hash`；不要修改现有风险结果响应。

## 确定性边界

- `revision_key`绑定`review_id + generation_id + result_hash + finding_id`。
- Python从正式Finding的Evidence反查同代IR。只有唯一连续文本、唯一IR及唯一Anchor
  才允许`REPLACE`或`DELETE`。
- ABSENCE、多个不连续位置、缺失附件、外部授权材料和联动重构均返回
  `UNSUPPORTED`。
- 模型按最多6条确定性分批，仅返回`replacement_text`和可选`draft_note`；
  `temperature=0`、`thinking=false`、Tool=0、JSON Object。
- Provider Prompt目标为6,000 Token，超过7,000为该批生成失败。
- 单条校验失败进入`failed_findings`，不改变其他草案和正式风险结果。
- MVP缓存按`review_id/generation_id/result_hash`隔离，默认使用独立JSON目录，
  不写正式Result Sink。

## 状态与错误

状态：`PENDING`、`GENERATING`、`COMPLETED`、`PARTIAL_FAILED`、`FAILED`。

错误码：

- `REVIEW_NOT_FOUND`
- `REVIEW_NOT_COMPLETED`
- `GENERATION_NOT_FOUND`
- `RESULT_HASH_MISMATCH`
- `FINDING_NOT_FOUND`
- `FINDING_SOURCE_NOT_UNIQUE`
- `SOURCE_TEXT_INVALID`
- `REVISION_GENERATION_FAILED`

`draft.finding_id`与原风险结果中的`finding_id`完全一致，前端可直接建立索引并在
原风险卡片下展示Diff、接受或拒绝按钮。本阶段不写入ONLYOFFICE。
