**历史辅助脚本**

`generate_stage5_pdf.py` 是旧阶段环境的 PDF 生成助手，依赖 `/workspace`、`/artifacts` 路径，以及额外提供的 `pdf_factory` 导入路径。仓库中未发现调用方。本轮从 scripts 根目录归档，保留原文以供追溯，不作为当前通用命令。

当前 PDF 测试使用 `services/contract/tests/pdf_factory.py`。`scripts/contract_window_stage5_e2e.sh` 和 Contract 的阶段脚本仍可用于专门配置的验收环境，未随本次归档移动。
