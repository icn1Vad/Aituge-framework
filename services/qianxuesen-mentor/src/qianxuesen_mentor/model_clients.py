from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import AsyncIterator
from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx
from aituge_model.config import ModelRuntimeProvider

from qianxuesen_mentor.config import Settings
from qianxuesen_mentor.errors import QianXuesenError


def _headers(api_key: str, component_id: str, via_gateway: bool) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if via_gateway:
        headers["X-Aituge-Model-Component-ID"] = component_id
    return headers


class ModelClients:
    embedding_batch_size = 10

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self.settings = settings
        provider = ModelRuntimeProvider.from_environment(
            directory=str(settings.model_config_dir or ""), pack_id=settings.model_pack_id,
            secret_dir=str(settings.model_secret_dir or "") or None,
        )
        self.llm_config = provider.resolve_llm()
        self.embedding_config = provider.resolve_embedding()
        self.reranker_config = provider.resolve_reranker()
        identity = self.embedding_config.identity_base_url or self.embedding_config.base_url
        profile_source = (
            f"openai_compatible|{identity.rstrip('/')}|"
            f"{self.embedding_config.model}|{self.embedding_config.dimensions}"
        )
        self.profile_id = hashlib.sha256(
            profile_source.encode("utf-8")
        ).hexdigest()[:24]
        self._http = http_client or httpx.Client()

    async def should_retrieve(
        self,
        question: str,
        recent_history: list[dict[str, str]],
    ) -> bool:
        history_text = "\n".join(
            f"{item['role']}: {item['content'][:800]}" for item in recent_history[-4:]
        ) or "（无）"
        messages = [
            {
                "role": "system",
                "content": (
                    "你是钱学森导师问答的路由器。判断回答当前消息前是否必须查询钱学森资料库。\n"
                    "只有涉及钱学森的具体生平、日期、著作、原话、出处、科学思想，或明确要求依据其理论和资料论证时，输出 RETRIEVE。\n"
                    "寒暄、感谢、情绪交流、一般学习生活建议、开放讨论，以及凭当前对话即可自然回答的问题，输出 DIRECT。\n"
                    "多数日常导师对话应直接回答。只输出 RETRIEVE 或 DIRECT。"
                ),
            },
            {
                "role": "user",
                "content": f"最近对话：\n{history_text}\n\n当前消息：{question}",
            },
        ]
        decision = (await self._complete_chat(messages, max_tokens=8)).strip().upper()
        return decision.startswith("RETRIEVE")

    async def plan_tool(
        self,
        question: str,
        recent_history: list[dict[str, str]],
        *,
        web_search_available: bool,
        code_interpreter_available: bool,
    ) -> dict[str, str]:
        tools = [
            "qxs_retrieve：查询钱学森生平、日期、著作、原话、出处、科学思想和内部书籍资料。",
        ]
        if web_search_available:
            tools.append(
                "web_search：查询新闻、天气、现任人物、近期政策、当前产品或其他会变化的外部信息。"
            )
        if code_interpreter_available:
            tools.append(
                "code_interpreter：运行 Python 做精确计算、验证程序、数据分析或绘图；只是撰写代码而不要求运行时不必调用。"
            )
        history_text = "\n".join(
            f"{item['role']}: {item['content'][:800]}" for item in recent_history[-4:]
        ) or "（无）"
        messages = [
            {
                "role": "system",
                "content": (
                    "你是钱学森导师智能体的工具规划器。根据当前问题选择至多一个必要工具。\n"
                    "普通寒暄、情绪支持、一般知识和凭模型即可回答的问题使用 direct。\n"
                    "不要为了展示能力而调用工具。需要工具时生成可直接执行的参数；Python 代码必须 print 最终结果。\n"
                    f"当前日期：{date.today().isoformat()}。\n"
                    "可用能力：\n- " + "\n- ".join(tools) + "\n"
                    "只输出一个 JSON 对象，不要解释或使用 Markdown。格式为：\n"
                    '{"tool":"direct|qxs_retrieve|web_search|code_interpreter",'
                    '"query":"检索词", "code":"Python代码"}'
                ),
            },
            {
                "role": "user",
                "content": f"最近对话：\n{history_text}\n\n当前问题：{question}",
            },
        ]
        content = await self._complete_chat(messages, max_tokens=1000)
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return {"tool": "direct", "query": "", "code": ""}
        try:
            payload = json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            return {"tool": "direct", "query": "", "code": ""}
        tool = str(payload.get("tool") or "direct")
        allowed = {"direct", "qxs_retrieve"}
        if web_search_available:
            allowed.add("web_search")
        if code_interpreter_available:
            allowed.add("code_interpreter")
        if tool not in allowed:
            tool = "direct"
        return {
            "tool": tool,
            "query": str(payload.get("query") or question).strip()[:400],
            "code": str(payload.get("code") or "").strip()[:30_000],
        }

    async def _complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
    ) -> str:
        cfg = self.llm_config
        endpoint = cfg.base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if _is_official_deepseek_v4(cfg):
            payload["thinking"] = {"type": "disabled"}
        else:
            payload["enable_thinking"] = False
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        timeout_seconds = min(30.0, self.settings.llm_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.post(
                    endpoint,
                    headers={
                        **_headers(cfg.api_key, cfg.id, cfg.via_gateway),
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise TypeError("completion content must be a string")
                return content
        except (httpx.HTTPError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise QianXuesenError(
                "router_unavailable",
                "问题路由暂时不可用",
                status_code=503,
                details={"reason": type(exc).__name__},
            ) from exc

    async def stream_chat(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        cfg = self.llm_config
        endpoint = cfg.base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": self.settings.llm_answer_temperature,
            "max_tokens": min(cfg.max_tokens, self.settings.llm_answer_max_tokens),
            "stream": True,
        }
        if _is_official_deepseek_v4(cfg):
            payload["thinking"] = {"type": "enabled" if cfg.enable_thinking else "disabled"}
        else:
            payload["enable_thinking"] = cfg.enable_thinking
            payload["chat_template_kwargs"] = {"enable_thinking": cfg.enable_thinking}
        timeout = httpx.Timeout(
            self.settings.llm_timeout_seconds,
            connect=min(30.0, self.settings.llm_timeout_seconds),
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    headers={
                        **_headers(cfg.api_key, cfg.id, cfg.via_gateway),
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise QianXuesenError(
                            "llm_request_failed",
                            "回答模型暂时不可用",
                            status_code=503,
                            details={"status_code": response.status_code},
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            body = json.loads(data)
                            delta = body["choices"][0]["delta"].get("content")
                        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                            continue
                        if isinstance(delta, str) and delta:
                            yield delta
        except QianXuesenError:
            raise
        except httpx.HTTPError as exc:
            raise QianXuesenError(
                "llm_unavailable",
                "回答模型连接失败",
                status_code=503,
                details={"reason": type(exc).__name__},
            ) from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors, _ = self.embed_with_usage(texts)
        return vectors

    def embed_with_usage(self, texts: list[str]) -> tuple[list[list[float]], int]:
        if not texts:
            return [], 0
        vectors: list[list[float]] = []
        input_tokens = 0
        for start in range(0, len(texts), self.embedding_batch_size):
            batch = texts[start:start + self.embedding_batch_size]
            batch_vectors, batch_tokens = self._embed_batch(batch)
            vectors.extend(batch_vectors)
            input_tokens += batch_tokens
        return vectors, input_tokens

    def _embed_batch(self, texts: list[str]) -> tuple[list[list[float]], int]:
        cfg = self.embedding_config
        endpoint = cfg.base_url.rstrip("/")
        if not endpoint.endswith("/embeddings"):
            endpoint += "/embeddings"
        payload = {
            "model": cfg.model, "input": texts, "dimensions": cfg.dimensions, "encoding_format": "float",
        }
        attempts = 1 if cfg.via_gateway else 3
        last_reason = ""
        for attempt in range(attempts):
            try:
                response = self._http.post(
                    endpoint, headers=_headers(cfg.api_key, cfg.id, cfg.via_gateway),
                    json=payload, timeout=cfg.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                last_reason = type(exc).__name__
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise QianXuesenError(
                    "embedding_unavailable", "Embedding API 请求失败", status_code=503,
                    details={"reason": last_reason},
                ) from exc
            if response.status_code == 429 or response.status_code >= 500:
                last_reason = f"HTTP {response.status_code}"
                if attempt + 1 < attempts:
                    time.sleep(_retry_delay(response, attempt))
                    continue
                raise QianXuesenError(
                    "embedding_unavailable", "Embedding API 暂时不可用", status_code=503,
                    details={"reason": last_reason},
                )
            if response.status_code >= 400:
                raise QianXuesenError(
                    "embedding_request_rejected", "Embedding API 拒绝了请求", status_code=422,
                    details={"status_code": response.status_code},
                )
            try:
                body = response.json()
                batch_vectors = _validate_embedding_response(
                    body, expected_count=len(texts), dimensions=cfg.dimensions,
                )
                usage = body.get("usage") or {}
                input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            except (KeyError, OverflowError, TypeError, ValueError) as exc:
                raise QianXuesenError(
                    "embedding_invalid_response", "Embedding API 返回内容不合法", status_code=502,
                ) from exc
            return batch_vectors, input_tokens
        raise AssertionError(last_reason or "unreachable")

    def rerank(self, query: str, candidates: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
        if not candidates:
            return []
        cfg = self.reranker_config
        endpoint = cfg.base_url.rstrip("/")
        if not endpoint.endswith(("/rerank", "/reranks")):
            endpoint += "/reranks"
        response = self._http.post(endpoint, headers=_headers(cfg.api_key, cfg.id, cfg.via_gateway), json={
            "model": cfg.model, "query": query,
            "documents": [f"{item.get('title','')}\n{item.get('content','')}" for item in candidates],
            "top_n": min(top_n, len(candidates)), "instruct": cfg.instruction,
        }, timeout=cfg.timeout_seconds)
        response.raise_for_status()
        output = []
        for item in response.json().get("results", []):
            candidate = dict(candidates[int(item["index"])])
            candidate["rerank_score"] = float(item["relevance_score"])
            output.append(candidate)
        return output


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    try:
        retry_after = float(response.headers.get("Retry-After", ""))
    except ValueError:
        retry_after = 0
    return min(max(retry_after, 0.5 * (2**attempt)), 10.0)


def _is_official_deepseek_v4(config: Any) -> bool:
    provider = str(getattr(config, "provider", "")).lower()
    hostname = (urlparse(str(getattr(config, "base_url", ""))).hostname or "").lower()
    model = str(getattr(config, "model", "")).lower()
    return (provider == "deepseek" or hostname == "api.deepseek.com") and model.startswith("deepseek-v4-")


def _validate_embedding_response(body: Any, *, expected_count: int, dimensions: int) -> list[list[float]]:
    if not isinstance(body, dict):
        raise TypeError("embedding response must be an object")
    data = body.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise ValueError("embedding response count mismatch")
    vectors: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise TypeError("embedding item must be an object")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("embedding index must be an integer")
        if index < 0 or index >= expected_count or index in vectors:
            raise ValueError("embedding index is invalid")
        raw = item.get("embedding")
        if not isinstance(raw, list) or len(raw) != dimensions:
            raise ValueError("embedding dimension mismatch")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw):
            raise TypeError("embedding value must be numeric")
        vector = [float(value) for value in raw]
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("embedding value must be finite")
        vectors[index] = vector
    if set(vectors) != set(range(expected_count)):
        raise ValueError("embedding indexes are incomplete")
    return [vectors[index] for index in range(expected_count)]
