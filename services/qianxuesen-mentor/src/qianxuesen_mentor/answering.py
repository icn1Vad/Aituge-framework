from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from qianxuesen_mentor.errors import QianXuesenError
from qianxuesen_mentor.model_clients import ModelClients
from qianxuesen_mentor.retrieval import RetrievalService
from qianxuesen_mentor.schemas import ChatHistoryItem
from qianxuesen_mentor.tools_runtime import CodeInterpreterTool, WebSearchTool


SYSTEM_PROMPT = """请以钱学森式的科学家导师口吻同用户自然交谈，直接进入问题，不作身份说明。

语气冷静、朴实而有判断力。既能从系统、关系和实践中讲清道理，也能用凝练、有启发性的句子点出问题的关键。把资料化成自己的理解来谈，不要像检索报告，也不必套固定结构；简单问题简洁回答，深问题可以深入讨论、反问和引导。

寒暄保持简短自然，但不必总用同一句式；可根据最近的对话承接用户的语气。

遇到错误前提先自然纠正；具体事实不能确定时，说明判断边界，再给出相关原理、可行办法或核查路径，不空转，也不把“资料中未找到”说成绝对不存在。

本轮提供的资料是史实和出处的依据。涉及关键史实时可自然标注 [E1] 这样的证据编号，其余部分保持一次真实、连贯的导师对话。
"""

CONTEXTUAL_FOLLOWUP = re.compile(r"^(那|那么|这个|上述|其中|具体|为什么|怎么|如何|哪|他|这)")
RETRIEVAL_FALLBACK = re.compile(
    r"钱学森|系统工程|工程控制论|航天|导弹|原话|原文|出处|哪一页|著作|文集|生平"
)


class MentorAnswerService:
    def __init__(
        self,
        retrieval: RetrievalService,
        models: ModelClients | None,
        web_search: WebSearchTool | None = None,
        code_interpreter: CodeInterpreterTool | None = None,
    ) -> None:
        self.retrieval = retrieval
        self.models = models
        self.web_search = web_search
        self.code_interpreter = code_interpreter

    async def stream(
        self,
        question: str,
        *,
        history: Sequence[ChatHistoryItem],
        top_k: int,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        request_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        sequence = 0
        yield "meta", {
            "requestId": request_id,
            "conversationId": "experience",
            "messageId": message_id,
            "sequence": sequence,
            "resumed": False,
        }
        sequence += 1

        if self.models is None:
            raise QianXuesenError("llm_unconfigured", "回答模型尚未配置", status_code=503)
        plan = await self._plan_tool(question, history)
        tool_name = plan.get("tool", "direct")
        citations: list[dict[str, Any]] = []
        messages = _direct_messages(question, history)

        if tool_name == "qxs_retrieve":
            tool_call_id = str(uuid.uuid4())
            retrieval_query = _retrieval_query(plan.get("query") or question, history)
            yield "tool", _tool_event(
                tool_call_id, "钱学森知识检索", "started", sequence,
                arguments={"query": retrieval_query, "top_k": top_k},
                message="正在检索确定信息、工程方法和书籍原文",
            )
            sequence += 1
            retrieval = await asyncio.to_thread(
                self.retrieval.search,
                retrieval_query,
                top_k=top_k,
                retrieval_mode="hybrid",
            )
            evidence = retrieval.get("results") or []
            evidence_chars = sum(len(str(item.get("quote") or "")) for item in evidence)
            yield "tool", _tool_event(
                tool_call_id, "钱学森知识检索", "completed", sequence,
                result_chars=evidence_chars,
                message=f"已找到 {len(evidence)} 条可核验资料",
            )
            sequence += 1
            messages = _build_messages(question, history, evidence[:6])
            citations = _book_citations(evidence)

        elif tool_name == "web_search":
            tool_call_id = str(uuid.uuid4())
            search_query = plan.get("query") or question
            yield "tool", _tool_event(
                tool_call_id, "网页搜索", "started", sequence,
                arguments={"query": search_query}, message="正在搜索最新网页资料",
            )
            sequence += 1
            web_results: list[dict[str, str]] = []
            search_error = ""
            try:
                if self.web_search is not None:
                    web_results = await self.web_search.search(search_query)
                else:
                    search_error = "网页搜索工具未配置"
            except Exception as exc:
                search_error = f"网页搜索暂时失败（{type(exc).__name__}）"
            result_chars = sum(len(item.get("snippet", "")) for item in web_results)
            message = f"已找到 {len(web_results)} 条网页结果" if web_results else search_error or "未找到网页结果"
            yield "tool", _tool_event(
                tool_call_id, "网页搜索", "completed", sequence,
                result_chars=result_chars, message=message,
            )
            sequence += 1
            messages = _web_messages(question, history, web_results, search_error)
            citations = _web_citations(web_results)

        elif tool_name == "code_interpreter":
            code = plan.get("code") or ""
            if code:
                tool_call_id = str(uuid.uuid4())
                yield "tool", _tool_event(
                    tool_call_id, "Python 代码执行", "started", sequence,
                    arguments={"code": code}, message="正在运行代码并核验结果",
                )
                sequence += 1
                try:
                    if self.code_interpreter is None:
                        result = {"exit_code": None, "stdout": "", "stderr": "", "error": "代码执行工具未配置"}
                    else:
                        result = await self.code_interpreter.execute(code)
                except Exception as exc:
                    result = {"exit_code": None, "stdout": "", "stderr": "", "error": f"代码执行失败（{type(exc).__name__}）"}
                result_chars = len(str(result.get("stdout") or "")) + len(str(result.get("stderr") or ""))
                outcome = "代码执行完成" if result.get("exit_code") == 0 else str(result.get("error") or "代码运行出错")
                yield "tool", _tool_event(
                    tool_call_id, "Python 代码执行", "completed", sequence,
                    result_chars=result_chars, message=outcome,
                )
                sequence += 1
                messages = _code_messages(question, history, code, result)

        async for delta in self.models.stream_chat(messages):
            yield "delta", {"content": delta, "sequence": sequence, "resumed": False}
            sequence += 1

        for citation in citations:
            yield "citation", {"citation": citation}

        yield "done", {
            "messageId": message_id,
            "sequence": sequence,
            "resumed": False,
        }

    async def _plan_tool(
        self,
        question: str,
        history: Sequence[ChatHistoryItem],
    ) -> dict[str, str]:
        if self.models is not None:
            try:
                return await self.models.plan_tool(
                    question,
                    [{"role": item.role, "content": item.content} for item in history[-4:]],
                    web_search_available=bool(self.web_search and self.web_search.enabled),
                    code_interpreter_available=bool(
                        self.code_interpreter and self.code_interpreter.available
                    ),
                )
            except Exception:
                pass
        context = "\n".join(item.content for item in history[-2:])
        tool = "qxs_retrieve" if RETRIEVAL_FALLBACK.search(f"{context}\n{question}") else "direct"
        return {"tool": tool, "query": question, "code": ""}


def _build_messages(
    question: str,
    history: Sequence[ChatHistoryItem],
    evidence: Sequence[dict[str, Any]],
) -> list[dict[str, str]]:
    evidence_blocks: list[str] = []
    for index, item in enumerate(evidence, start=1):
        page_start = item.get("page_start")
        page_end = item.get("page_end")
        page = str(page_start) if page_start == page_end or not page_end else f"{page_start}-{page_end}"
        marker = f"[《{item.get('document_name')}》｜第{page}页｜Chunk #{item.get('chunk_id')}]"
        evidence_blocks.append("\n".join([
            f"证据 E{index}（{item.get('evidence_type')}）：{marker}",
            f"章节：{item.get('chapter') or '未标注'}",
            f"内容：{str(item.get('quote') or '')[:1000]}",
        ]))
    user_prompt = (
        "下面是与问题相关的资料。请先判断资料是否真正支持问题，再吸收有用内容自然回答；关键史实可用 [E1]、[E2] 标注出处，不必逐条复述资料。\n\n"
        + "\n\n".join(evidence_blocks)
        + f"\n\n本轮问题：{question}"
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history[-8:]:
        messages.append({"role": item.role, "content": item.content})
    messages.append({"role": "user", "content": user_prompt})
    return messages


def _direct_messages(
    question: str,
    history: Sequence[ChatHistoryItem],
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history[-8:]:
        messages.append({"role": item.role, "content": item.content})
    messages.append({"role": "user", "content": question})
    return messages


def _web_messages(
    question: str,
    history: Sequence[ChatHistoryItem],
    results: Sequence[dict[str, str]],
    error: str,
) -> list[dict[str, str]]:
    blocks = [
        "\n".join([
            f"网页 W{index}：{item.get('title') or '未命名页面'}",
            f"地址：{item.get('url') or ''}",
            f"摘要：{item.get('snippet') or '无摘要'}",
        ])
        for index, item in enumerate(results, start=1)
    ]
    status = error or ("已取得网页搜索结果" if results else "网页搜索没有返回结果")
    prompt = (
        f"网页工具状态：{status}。\n"
        "请依据真正相关的网页结果回答当前问题，实时事实可用 [W1]、[W2] 标注；"
        "搜索结果只是摘要，若不足以支持结论就说明边界。\n\n"
        + "\n\n".join(blocks)
        + f"\n\n本轮问题：{question}"
    )
    return _messages_with_history(history, prompt)


def _code_messages(
    question: str,
    history: Sequence[ChatHistoryItem],
    code: str,
    result: dict[str, Any],
) -> list[dict[str, str]]:
    prompt = (
        "下面是为当前问题实际运行的 Python 代码和结果。请以执行结果为准，"
        "简洁回答并解释必要的计算过程；如果执行失败，说明失败点，不要虚构结果。\n\n"
        f"代码：\n```python\n{code}\n```\n\n"
        f"执行结果：\n{json.dumps(result, ensure_ascii=False)}\n\n"
        f"本轮问题：{question}"
    )
    return _messages_with_history(history, prompt)


def _messages_with_history(
    history: Sequence[ChatHistoryItem],
    user_prompt: str,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in history[-8:]:
        messages.append({"role": item.role, "content": item.content})
    messages.append({"role": "user", "content": user_prompt})
    return messages


def _tool_event(
    tool_call_id: str,
    name: str,
    status: str,
    sequence: int,
    *,
    arguments: dict[str, Any] | None = None,
    result_chars: int | None = None,
    message: str,
) -> dict[str, Any]:
    return {
        "toolCallId": tool_call_id,
        "name": name,
        "status": status,
        "arguments": json.dumps(arguments, ensure_ascii=False) if arguments is not None else None,
        "resultChars": result_chars,
        "message": message,
        "sequence": sequence,
        "resumed": False,
    }


def _book_citations(evidence: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for item in _citation_items(evidence):
        page_start = item.get("page_start")
        page_end = item.get("page_end")
        location = f"第 {page_start} 页"
        if page_end and page_end != page_start:
            location = f"第 {page_start}–{page_end} 页"
        citations.append({
            "sourceId": str(item.get("document_id") or "") or None,
            "sourceName": item.get("document_name"),
            "location": location,
            "excerpt": str(item.get("quote") or "")[:360] or None,
        })
    return citations


def _web_citations(results: Sequence[dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "sourceId": item.get("url") or None,
            "sourceName": item.get("title") or "网页来源",
            "location": "网页搜索",
            "excerpt": (item.get("snippet") or "")[:360] or None,
        }
        for item in results[:6]
    ]


def _citation_items(evidence: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in evidence:
        key = (
            str(item.get("document_id") or ""),
            str(item.get("page_start") or ""),
            str(item.get("chunk_id") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
        if len(output) >= 6:
            break
    return output


def _retrieval_query(question: str, history: Sequence[ChatHistoryItem]) -> str:
    if not history or (len(question) > 18 and not CONTEXTUAL_FOLLOWUP.search(question)):
        return question
    previous_user = next(
        (item.content for item in reversed(history) if item.role == "user"),
        "",
    )
    return f"{previous_user}\n追问：{question}" if previous_user else question
