from __future__ import annotations

import asyncio
import base64

from qianxuesen_mentor.config import Settings
from qianxuesen_mentor.tools_runtime import (
    CodeInterpreterTool,
    _BingResultsParser,
)


def test_bing_parser_extracts_normalized_result():
    target = "https://www.cas.cn/example"
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    html = (
        '<ol><li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1'
        + encoded
        + '">钱学森简介</a></h2><div class="b_caption"><p>中国科学院 资料摘要。</p></div></li></ol>'
    )
    parser = _BingResultsParser(limit=5)
    parser.feed(html)
    assert [item.to_dict() for item in parser.results] == [{
        "title": "钱学森简介",
        "url": target,
        "snippet": "中国科学院 资料摘要。",
    }]


def test_code_interpreter_executes_python_and_cleans_up():
    async def run():
        tool = CodeInterpreterTool(Settings(
            _env_file=None,
            code_execution_timeout_seconds=5,
            code_execution_max_output_chars=2000,
        ))
        return await tool.execute("print(sum(i * i for i in range(6)))")

    result = asyncio.run(run())
    assert result["exit_code"] == 0
    assert result["stdout"] == "55"
    assert result["stderr"] == ""
    assert result["error"] is None


def test_code_interpreter_reports_failure():
    async def run():
        tool = CodeInterpreterTool(Settings(_env_file=None))
        return await tool.execute("raise ValueError('bad input')")

    result = asyncio.run(run())
    assert result["exit_code"] == 1
    assert "ValueError: bad input" in result["stderr"]
