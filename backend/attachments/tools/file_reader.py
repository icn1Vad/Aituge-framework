"""Agent tool: read the extracted text of an attached file.

Registration is cheap — only the name → id mapping is captured in closure.
Content is fetched at each tool call so extraction that completes after
`parse_attachment_tools` has already run is still visible. If extraction
is still in flight at call time the tool briefly polls before giving up
with a clear "still processing" message rather than lying "file is empty".
"""
import asyncio
import json
from typing import Annotated, List

from llama_index.core.tools import FunctionTool
from loguru import logger

from backend.attachments.support import FileStatus
from db.db_context import create_db_session
from backend.attachments.service.file_resource_service import FileResourceService


# Poll the extraction state for up to this long when the file is still
# parsing at tool-call time. Covers the common race where the user sends
# a chat message before the worker has finished processing a freshly-uploaded
# attachment. Longer than typical PDF extraction (~1–3s on a few-MB file) but
# short enough that a broken/stuck extraction doesn't hang the agent.
POLL_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.5

# Mirror of FileResourceService.LLM_INLINE_TEXT_LIMIT — trims what the tool
# returns into the LLM context so a 500KB extract doesn't blow the window.
INLINE_TEXT_LIMIT = FileResourceService.LLM_INLINE_TEXT_LIMIT


async def aget_file_reader(file_ids: List[str], tenant_id: str):
    if not file_ids:
        raise ValueError("file_ids is required")

    # Name → id mapping is stable across the chat turn; pre-fetch so the
    # tool doesn't have to.
    async with create_db_session() as session:
        svc = FileResourceService(session)
        files = await svc.get_files(file_ids=list(file_ids), tenant_id=tenant_id)
    name_to_id = {f.id: f.file_name for f in files}

    async def aread_file_content(file_id: str, offset: int = 0, limit: int = INLINE_TEXT_LIMIT):
        file_name = name_to_id.get(file_id)
        if file_name is None:
            return json.dumps({"error": "附件不属于本轮快照", "available_file_ids": list(name_to_id)}, ensure_ascii=False)
        offset, limit = max(0, offset), max(1, min(limit, 50000))
        deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT_SECONDS
        while True:
            async with create_db_session() as session:
                svc = FileResourceService(session)
                entity = await svc.get_file(file_id=file_id, tenant_id=tenant_id)
                if entity is None:
                    return json.dumps(
                        {"error": f"文件 '{file_name}' 不存在"},
                        ensure_ascii=False,
                    )

                if entity.status == FileStatus.failed.value:
                    return json.dumps({
                        "error": f"文件 '{file_name}' 解析失败: {entity.failed_reason or '未知原因'}",
                    }, ensure_ascii=False)

                row = await svc.get_text_content(
                    file_id=file_id, tenant_id=tenant_id
                )
                if row is not None and row.content:
                    result = await svc.get_text_slice(file_id=file_id, tenant_id=tenant_id, offset=offset, limit=limit)
                    result["file_name"] = file_name
                    return json.dumps(result, ensure_ascii=False)

                # No content yet. If extraction has already reported a
                # terminal success, this is a truly empty / non-text file —
                # return that honestly instead of looping forever.
                if entity.status in (
                    FileStatus.succeeded.value,
                    FileStatus.cancelled.value,
                ):
                    return json.dumps({
                        "data": f"📄 文件“{file_name}” 未提取到可读取的文字，不能据此判断图片画面或视频内容。",
                    }, ensure_ascii=False)

            # Still pending/parsing/persisting — wait a bit and retry.
            if asyncio.get_event_loop().time() >= deadline:
                logger.warning(
                    f"[file_reader] timed out waiting for {file_id} ({file_name}) "
                    f"after {POLL_TIMEOUT_SECONDS}s; status={entity.status}"
                )
                return json.dumps({
                    "warning": (
                        f"文件 '{file_name}' 仍在解析中（已等待 "
                        f"{POLL_TIMEOUT_SECONDS:.0f} 秒），请稍后重试。"
                    ),
                }, ensure_ascii=False)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    return FunctionTool.from_defaults(
        async_fn=aread_file_content,
        name="read-file",
        description="按稳定 file_id 读取本轮附件文字；offset 和 limit 用于分页，has_more 表示后面还有内容。",
        return_direct=False,
    )
