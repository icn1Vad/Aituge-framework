import json
from typing import Optional, List
from llama_index.core.tools import FunctionTool
import traceback
from typing import Any
PaiLlm = Any
from loguru import logger


async def analyze_multimodal(
    image_base64_list: List[str],
    video_base64_list: List[str],
    question: str = "",
    multimodal_llm: PaiLlm = None,
) -> str:
    data_list = []
    if image_base64_list:
        # OpenAI-compatible requests use nested url objects.
        data_list.extend([{"type": "image_url", "image_url": {"url": image}} for image in image_base64_list])
    if video_base64_list:
        data_list.extend([{"type": "video_url", "video_url": {"url": video}, "fps": 2} for video in video_base64_list])

    system_prompt = (
        "你是一个图片和视频多模态数据理解专家。"
        "请结合用户输入的问题，对图片和视频生成尽量简洁明确的描述，不超过200字。"
    )
    user_prompt = question or "请描述图片和视频中的内容。"
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": system_prompt},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                *data_list,
            ],
        }
    ]
    try:
        response = ""
        response_gen = await multimodal_llm.astream(messages)
        async for chunk in response_gen:
            response += chunk.delta
        return response
    except Exception as e:
        logger.error(f"解析图片和视频出错: {traceback.format_exc()}")
        return json.dumps({
            "error": f"解析图片和视频出错: {str(e)}"
        }, ensure_ascii=False)


async def aget_multimodal_analysis_from_db(
    image_list: List[str],
    video_list: List[str],
    question: Optional[str] = None,
    multimodal_llm: PaiLlm = None,
) -> str:
    if not image_list and not video_list:
        return json.dumps({"error": "无法获取图片或者视频访问链接"}, ensure_ascii=False)

    try:
        answer = await analyze_multimodal(image_base64_list=image_list, video_base64_list=video_list, question=question, multimodal_llm=multimodal_llm)

        return json.dumps({
            "question": question,
            "answer": answer,
        }, ensure_ascii=False)

    except Exception as e:
        logger.error(f"VLM 解析失败: {str(e)}")
        return json.dumps({
            "error": f"VLM analysis failed: {str(e)}"
        }, ensure_ascii=False)


class FrameworkVisionClient:
    """PAI stream adapter backed by the shared model registry/gateway."""
    def __init__(self, config):
        self.config = config
    async def astream(self, messages):
        import httpx
        from types import SimpleNamespace
        async def stream():
            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(self.config.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.config.api_key}"},
                    json={"model": self.config.model, "messages": messages, "max_tokens": self.config.max_tokens, "stream": False})
                response.raise_for_status()
                yield SimpleNamespace(delta=response.json()["choices"][0]["message"]["content"])
        return stream()
