# 会话附件

本模块管理附件原件、元数据、解析文本、分块、分片上传、引用及过期清理，提供文件 API 和 Agent 附件工具。附件仅用于会话，不自动进入正式制度库。

## 当前默认路线

- JPG/PNG 等支持的图片和 PDF 上传后由本地 MinerU 解析；文本、Office、表格使用对应解析器。
- Scheduler 根据本次任务的附件快照挂载 `read-file` 和 `search-file-chunks`，同时注入附件目录和使用说明。快照包括该会话截至当前消息已发送的附件。
- 图片和文档统一通过 `read-file` 读取保存的文字结果；长文件分页读取或搜索分块。没有可读文字时如实说明，不能推断画面。
- 表格计算仍使用原件及现有代码执行工具。

## 视觉理解工具：保留实现，暂不挂载

`multimodal-parser` 的实现仍保留在 `tools/__init__.py` 和 `tools/multimodal_parser.py`，能够读取图片或视频原件并调用云端视觉模型，默认模型为百炼 `qwen3-vl-plus`，凭证复用统一模型配置。

它目前不是主要工具，默认附件工具组不挂载它，Agent 的附件提示及无文字返回也不再推荐它。因此当前聊天不会通过此工具分析照片画面或视频。

需要单独调试或后续恢复时，可显式调用 `create_attachment_bundle(attachments, tenant_id, model_pack_id, include_visual=True)`，再从返回的工具组中调用 `multimodal-parser(file_ids, query)`。生产 Scheduler 当前不传该选项。调用仍须满足附件快照、解析状态和模型凭证条件；恢复为主要能力前须同步调整使用提示并验收。

## 并发与部署

- 上传解析使用 Framework 现有 TaskManager/Worker 的持久化任务、租约和配额机制。`TASK_WORKER_CONCURRENCY` 控制单 Worker 同时执行的任务数；部署 Compose 默认 4，运行入口未配置时默认 1。
- Agent 内部调用附件工具与普通工具使用同一执行循环；同一步产生多个工具调用时统一并行执行。Worker 的任务并发数不等于 Agent 内工具调用数。
- MinerU 独立运行，Framework 通过 `AITUGE_MINERU_ENDPOINT` 访问。`AITUGE_MINERU_BACKEND` 默认 `vlm-engine`，`AITUGE_MINERU_TIMEOUT_SECONDS` 默认 600，`AITUGE_MINERU_TOKEN` 可选。容器部署使用可达的服务地址。
- API 与 Worker 共用数据库和持久化文件存储；镜像、模型与挂载配置随部署配置管理。

来源与许可证保留在本目录的 `PAI-RAG-LICENSE` 及迁移说明中。
