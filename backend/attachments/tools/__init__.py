"""PAI-RAG attachment tool semantics adapted to Framework ToolBundle.

All lookups are keyed by file_id. The bundle only contains the immutable
attachment snapshot supplied by the task, never draft uploads or future turns.
"""
import base64
import json
import os
import tempfile
from pathlib import Path
from llama_index.core.tools import FunctionTool
from aituge_model.config import ModelRuntimeProvider
from db.db_context import create_db_session
from tool.bundle import ToolBundle
from ..service.file_resource_service import FileResourceService

async def create_attachment_bundle(attachments, tenant_id, model_pack_id="", *, include_visual=False):
    ids = list(dict.fromkeys(item["file_id"] for item in attachments))
    async with create_db_session() as session:
        svc = FileResourceService(session)
        rows = await svc.get_files(ids, tenant_id)
        by_id = {row.id: row for row in rows}
        missing = set(ids) - by_id.keys()
        if missing:
            raise ValueError(f"附件已不存在：{', '.join(sorted(missing))}")
        if any(row.status != "succeeded" for row in rows):
            raise ValueError("附件尚未识别完成或识别失败，请等待或重试")
        temp = tempfile.TemporaryDirectory(prefix="tuge-attachments-")
        input_files = {}
        catalog = []
        for fid in ids:
            row = by_id[fid]
            item = {"file_id": fid, "name": row.file_name, "mime_type": row.mime_type,
                    "truncated_at_extract": (row.file_metadata or {}).get("truncated_at_extract", False)}
            if row.file_extension in {".csv", ".xls", ".xlsx"}:
                name = fid + row.file_extension
                path = Path(temp.name) / name
                stream = await svc.read_bytes(fid, tenant_id)
                path.write_bytes(stream.read())
                input_files[name] = path
                item["code_path"] = name
            catalog.append(item)

    def require(file_id):
        if file_id not in by_id:
            raise ValueError("文件不属于本轮附件快照")
        return by_id[file_id]

    async def multimodal_parser(file_ids: list[str], query: str) -> str:
        """Understand uploaded images or videos visually, including photos without text."""
        content = [{"type": "text", "text": query}]
        async with create_db_session() as session:
            svc = FileResourceService(session)
            for fid in file_ids:
                row = require(fid)
                mime = row.mime_type or ""
                if not mime.startswith(("image/", "video/")):
                    raise ValueError("视觉工具只接收图片或视频")
                stream = await svc.read_bytes(fid, tenant_id)
                uri = f"data:{mime};base64," + base64.b64encode(stream.read()).decode()
                kind = "video_url" if mime.startswith("video/") else "image_url"
                entry = {"type": kind, kind: {"url": uri}}
                if kind == "video_url":
                    entry["fps"] = 2
                content.append(entry)
        provider = ModelRuntimeProvider.from_environment(pack_id=model_pack_id)
        config = provider.resolve_llm(os.getenv("AITUGE_ATTACHMENT_VISION_MODEL", "qwen3-vl-plus"))
        if not config.vision_support:
            raise ValueError("附件视觉模型未启用 vision_support")
        from .multimodal_parser import aget_multimodal_analysis_from_db, FrameworkVisionClient
        return await aget_multimodal_analysis_from_db(
            image_list=[item["image_url"]["url"] for item in content if item["type"] == "image_url"],
            video_list=[item["video_url"]["url"] for item in content if item["type"] == "video_url"],
            question=query, multimodal_llm=FrameworkVisionClient(config))

    from .file_reader import aget_file_reader
    from .file_chunk_searcher import aget_file_chunk_searcher
    search_catalog = [{"file_id": row.id, "file_name": row.file_name, "chunk_count": "按需查询"} for row in rows]
    tools = [
        await aget_file_reader(ids, tenant_id),
        await aget_file_chunk_searcher(tenant_id, search_catalog),
    ]
    # Kept callable for explicit integration; current chat tasks do not mount it.
    if include_visual:
        tools.append(FunctionTool.from_defaults(async_fn=multimodal_parser, name="multimodal-parser"))
    bundle = ToolBundle.from_tools(tools, cleanup=temp.cleanup)
    prompt = ("本轮可读取的会话附件如下（不属于正式制度库）。必须先用附件工具读取相关内容，不能声称未收到附件。"
              "图片（包括 JPG/PNG）、PDF 和其他文档统一使用 read-file 读取已解析的文字；长文件可分页或搜索。"
              "读取结果没有文字时，应说明未提取到可读文字，不能凭空推断图片画面或视频内容。"
              "表格计算从 code_path 读取原件，Excel 必须检查所有工作表；图表沿用代码执行产物发布。\n"
              + json.dumps(catalog, ensure_ascii=False))
    return bundle, prompt, input_files
