import asyncio
from pathlib import Path

from tool.local_runtime import (
    LimitedLocalPythonConfig,
    LimitedLocalPythonTool,
    create_limited_local_python_bundle,
    create_limited_local_python_tools,
)


def test_limited_local_python_executes_code():
    async def run():
        tool = LimitedLocalPythonTool(LimitedLocalPythonConfig(timeout_seconds=5))
        try:
            result = await tool.aexecute("print(sum(range(5)))")
        finally:
            await tool.acleanup()

        assert "exit_code: 0" in result
        assert "stdout:\n10" in result

    asyncio.run(run())


def test_limited_local_python_accepts_code_fences():
    async def run():
        tool = LimitedLocalPythonTool(LimitedLocalPythonConfig(timeout_seconds=5))
        try:
            result = await tool.aexecute({"code": "```python\nprint('ok')\n```"})
        finally:
            await tool.acleanup()

        assert "stdout:\nok" in result

    asyncio.run(run())


def test_limited_local_python_returns_stderr_and_exit_code():
    async def run():
        tool = LimitedLocalPythonTool(LimitedLocalPythonConfig(timeout_seconds=5))
        try:
            result = await tool.aexecute("raise ValueError('bad')")
        finally:
            await tool.acleanup()

        assert "exit_code: 1" in result
        assert "stderr:" in result
        assert "ValueError: bad" in result

    asyncio.run(run())


def test_limited_local_python_times_out():
    async def run():
        tool = LimitedLocalPythonTool(LimitedLocalPythonConfig(timeout_seconds=1))
        try:
            result = await tool.aexecute("import time\ntime.sleep(3)")
        finally:
            await tool.acleanup()

        assert "timeout:" in result

    asyncio.run(run())


def test_limited_local_python_cleanup_removes_owned_temp_dir():
    async def run():
        tool = LimitedLocalPythonTool()
        work_dir = tool.work_dir
        assert work_dir.exists()
        await tool.acleanup()
        assert not work_dir.exists()

    asyncio.run(run())


def test_limited_local_python_keeps_explicit_work_dir(tmp_path: Path):
    async def run():
        work_dir = tmp_path / "runner"
        tool = LimitedLocalPythonTool(LimitedLocalPythonConfig(work_dir=work_dir))
        try:
            result = await tool.aexecute("from pathlib import Path\nPath('x.txt').write_text('x')\nprint(Path('x.txt').read_text())")
        finally:
            await tool.acleanup()

        assert "stdout:\nx" in result
        assert work_dir.exists()
        assert (work_dir / "x.txt").exists()

    asyncio.run(run())


def test_limited_local_python_factory_names():
    tools, cleanup = create_limited_local_python_tools()

    assert [tool.metadata.name for tool in tools] == ["LimitedLocalPythonInterpreter"]
    assert callable(cleanup)


def test_limited_local_python_bundle():
    bundle = create_limited_local_python_bundle()

    assert [tool.metadata.name for tool in bundle.tools] == ["LimitedLocalPythonInterpreter"]
    assert len(bundle.cleanup_hooks) == 1

