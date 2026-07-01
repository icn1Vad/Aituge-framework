# 抖音账号全量数据报告任务说明

## 定位

`analytics.douyin.account_report.generate` 是 TaskManager 里的抖音账号运营报告任务。

第一版默认使用 `analysis_scope=all_data`，也就是根据账号当前所有可用数据分析，而不是只分析某一个月。这样可以避免账号上个月没有发布内容时，报告因为月度窗口为空而无法产出。

## 运行链路

```text
前端/旧业务 OpenAPI
  -> TaskManager 创建任务
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
