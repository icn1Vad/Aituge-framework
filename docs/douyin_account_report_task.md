# 抖音账号全量数据报告任务说明

## 定位

`analytics.douyin.account_report.generate` 是 TaskManager 里的抖音账号运营报告任务。

第一版默认使用 `analysis_scope=all_data`，也就是根据账号当前所有可用数据分析，而不是只分析某一个月。这样可以避免账号上个月没有发布内容时，报告因为月度窗口为空而无法产出。

## 运行链路

```text
前端/旧业务 OpenAPI
  -> TaskManager 创建任务
  -> 可选：legacy_douyin_api adapter 拉取旧系统账号/作品数据
  -> input schema 校验
  -> report-agent
  -> douyin-account-report skill
  -> 结构化 JSON 输出
  -> output schema 校验
  -> task result / events 保存
```

## 输入字段

核心输入：

```json
{
  "account_id": "demo-douyin-account",
  "account_name": "Demo Douyin Account",
  "platform": "douyin",
  "data_source": "payload",
  "analysis_scope": "all_data",
  "report_depth": "deep",
  "report_context": {},
  "metrics_summary": {},
  "content_items": [],
  "top_contents": [],
  "low_contents": [],
  "warnings": [],
  "missing_fields": []
}
```

如果要直接复用服务器旧系统 `/opt/media_military` 的抖音数据，可以只传：

```json
{
  "task_type": "analytics.douyin.account_report.generate",
  "input_payload": {
    "data_source": "legacy_douyin_api",
    "account_id": "acct_douyin_b3c184659b34",
    "analysis_scope": "all_data",
    "content_limit": 50
  }
}
```

TaskManager 会调用旧系统本机 API：

```text
GET http://127.0.0.1:8010/analytics/douyin/overview
GET http://127.0.0.1:8010/analytics/douyin/contents
```

然后自动填充 `report_context`、`metrics_summary`、`content_items`、`top_contents`、`low_contents`、`warnings` 和 `missing_fields`。

`analysis_scope` 支持：

```text
all_data      默认，分析全部可用数据，不要求 month
month         月报模式，必须传 month
custom_range  自定义时间段，必须传 date_start/date_end
```

第一版建议旧业务接入时先传 `all_data`，后续有稳定账号数据后再恢复月报模式。

## 边界

Agent 可以负责报告扩写、归纳、行动建议，但不能编造数据。

不能编造：

- 粉丝数、播放、点赞、评论、分享、收藏。
- 年龄、性别、地域、兴趣、流量来源。
- 评论原文、情绪、关键词、用户问题。
- 没有上期数据时的环比。
- 没有 benchmark 数据时的同类账号对比。
- 没有完整脚本文本和留存证据时的脚本质量评价。

缺数据时必须写进 `missing_fields`、`warnings` 或 `data_limitations`。

## 输出字段

输出是一个 JSON 对象：

```json
{
  "status": "success | partial_data | insufficient_data",
  "account_id": "",
  "account_name": "",
  "platform": "douyin",
  "analysis_scope": "all_data",
  "covered_period": {},
  "warnings": [],
  "missing_fields": [],
  "executive_summary": "",
  "key_metrics": {},
  "sections": {},
  "top_content_analysis": [],
  "low_content_analysis": [],
  "data_limitations": [],
  "next_month_actions": [],
  "export_markdown": ""
}
```

`export_markdown` 是给前端快速预览或后续导出复用的可读版本，不替代结构化字段。

## 旧前端兼容输出

旧前端 `MonthlyReportPanel` 固定读取：

```text
report_json.sections.conclusion
report_json.sections.audience_and_traffic
report_json.sections.interaction_and_comments
report_json.sections.content_and_script_clues
```

所以 TaskManager 会在结构化结果里额外写入：

```text
result_payload_json.structured.legacy_monthly_report
```

这个对象就是旧前端可直接展示的自然语言报告结构。旧前端接入新接口时，可以把它当成原来的 `report_json` 使用：

```ts
const structured = response.task.result_payload_json.structured;
const reportJson = structured.legacy_monthly_report;
```

这样页面展示的仍然是自然语言段落、指标卡片、要点列表和内容明细表，而不是原始 JSON。
