# 铁矿石报告 Demo 数据集

这是一套面向内部 Demo 和 Agent Pipeline 测试的真实来源数据快照。数据按“原始响应、整理数据、元数据、质量检查”分层保存，避免把第三方口径误当成同一指标。

## 直接使用的文件

- `processed/iron_ore_report_demo_input.json`：可直接作为日报或周报任务输入。
- `processed/china_iron_ore_futures_i0_demo_260d.csv`：最近 260 个交易日的铁矿石连续合约行情。
- `processed/global_iron_ore_price_fred_demo_60m.csv`：最近 60 个月 IMF/FRED 全球基准。
- `processed/global_iron_ore_price_world_bank_demo_60m.csv`：最近 60 个月世界银行 CFR 现货基准。
- `processed/news_snapshot.json`：权威机构和主要矿商的事实卡片。
- `quality/quality_report.json`：机器可读的数据质量检查结果。

## 重要口径

- `I0` 是数据提供方拼接的连续合约，不是单一可交割合约。
- 中国期货价格为人民币/吨，全球基准为名义美元/吨，未接入汇率前禁止直接计算价差。
- 全球两套月度序列的基准定义并不完全相同，只用于趋势交叉验证。
- 当前没有港口库存、钢材产量、海运费和品位升贴水；这些应由客户授权数据或后续可靠数据源补齐。

## 重建方式

本机复用了 E 盘已有 Python 环境，仅依赖其中已有的 `requests`，没有向 C 盘安装依赖：

```powershell
& 'E:\MyProjects\AIflamework\Aituge-framework\.venv\Scripts\python.exe' `
  'E:\MyProjects\iron&translate\scripts\build_demo_data.py'
```

每次重建都会覆盖同名快照，并在 `metadata/sources.json` 记录抓取时间、原始文件哈希、数据范围和使用限制。
