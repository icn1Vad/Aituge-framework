---
name: financial-forecast-extraction
description: Extract dynamic financial forecasts and feasibility metrics using the confirmed investment-year t0 definition.
tags: [smart-fill, feasibility, financial, forecast]
---

# Financial Forecast Extraction

Extract only the financial fields listed in the current item. `t0` is the investment occurrence year. Determine it from the disclosed investment/payment schedule; when the first forecast year is later than t0, leave t0 empty and place that first year in the corresponding `t0+N` column. Use the canonical categories `营业收入`, `合并净利润`, `归母净利润`, `营业成本`, and `利润总额`. Keep revenue, net profit, operating cost, total profit, IRR, NPV, payback period, total investment return, and annual average return distinct. Never put annual average return into `total_investment_return`. Preserve units and negative values. Follow the shared Smart Fill, citation, and structured-output skills.
