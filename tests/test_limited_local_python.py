import asyncio
import json
from pathlib import Path

from tool.artifacts import ArtifactRef
from tool.local_runtime import (
    LimitedLocalPythonConfig,
    LimitedLocalPythonTool,
    create_limited_local_python_bundle,
    create_limited_local_python_tools,
)


class RecordingPublisher:
    def __init__(self):
        self.published: list[tuple[str, int, str]] = []

    async def publish(self, source_path: Path, *, sequence: int, mime: str) -> ArtifactRef:
        self.published.append((source_path.name, sequence, mime))
        prefix = "image" if mime.startswith("image/") else "artifact"
        name = f"{prefix}-{sequence:03d}{source_path.suffix.lower()}"
        return ArtifactRef(
            id=f"artifact-{sequence}",
            name=name,
            mime=mime,
            url=f"/task-manager/artifacts/artifact-{sequence}/content",
        )


def parsed(result: str) -> dict:
    return json.loads(result)


def test_limited_local_python_executes_code():
    async def run():
        tool = LimitedLocalPythonTool(LimitedLocalPythonConfig(timeout_seconds=5))
        try:
            result = await tool.aexecute("print(sum(range(5)))")
        finally:
            await tool.acleanup()

        assert parsed(result) == {
            "exit_code": 0,
            "stdout": "10",
            "stderr": "",
            "error": None,
            "artifacts": [],
        }

    asyncio.run(run())


def test_limited_local_python_accepts_code_fences():
    async def run():
        tool = LimitedLocalPythonTool(LimitedLocalPythonConfig(timeout_seconds=5))
        try:
            result = await tool.aexecute({"code": "```python\nprint('ok')\n```"})
        finally:
            await tool.acleanup()

        assert parsed(result)["stdout"] == "ok"

    asyncio.run(run())


def test_limited_local_python_returns_stderr_and_exit_code():
    async def run():
        tool = LimitedLocalPythonTool(LimitedLocalPythonConfig(timeout_seconds=5))
        try:
            result = await tool.aexecute("raise ValueError('bad')")
        finally:
            await tool.acleanup()

        payload = parsed(result)
        assert payload["exit_code"] == 1
        assert "ValueError: bad" in payload["stderr"]

    asyncio.run(run())


def test_limited_local_python_times_out():
    async def run():
        tool = LimitedLocalPythonTool(LimitedLocalPythonConfig(timeout_seconds=1))
        try:
            result = await tool.aexecute("import time\ntime.sleep(3)")
        finally:
            await tool.acleanup()

        payload = parsed(result)
        assert payload["exit_code"] is None
        assert payload["error"] == "Execution exceeded 1 seconds."

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

        assert parsed(result)["stdout"] == "x"
        assert work_dir.exists()
        assert len(list(work_dir.glob("*/x.txt"))) == 1

    asyncio.run(run())


def test_limited_local_python_returns_artifact_metadata(tmp_path: Path):
    async def run():
        work_dir = tmp_path / "runner"
        publisher = RecordingPublisher()
        tool = LimitedLocalPythonTool(
            LimitedLocalPythonConfig(
                work_dir=work_dir,
                artifact_publisher=publisher,
                cleanup_run_dir=True,
            )
        )
        try:
            result = await tool.aexecute(
                "\n".join(
                    [
                        "from pathlib import Path",
                        "Path('chart.svg').write_text("
                        "'<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"160\" height=\"90\">'"
                        "'<rect width=\"160\" height=\"90\" fill=\"white\"/>'"
                        "'<circle cx=\"80\" cy=\"45\" r=\"30\" fill=\"#6941c6\"/>'"
                        "'</svg>'"
                        ")",
                        "Path('view.html').write_text('<h1>hello artifact</h1>')",
                        "print('created')",
                    ]
                )
            )
        finally:
            await tool.acleanup()

        payload = parsed(result)
        assert payload["stdout"] == "created"
        assert payload["error"] is None
        assert payload["artifacts"] == [
            {
                "id": "artifact-1",
                "name": "image-001.svg",
                "mime": "image/svg+xml",
                "url": "/task-manager/artifacts/artifact-1/content",
            },
            {
                "id": "artifact-2",
                "name": "artifact-002.html",
                "mime": "text/html",
                "url": "/task-manager/artifacts/artifact-2/content",
            },
        ]
        assert publisher.published == [
            ("chart.svg", 1, "image/svg+xml"),
            ("view.html", 2, "text/html"),
        ]
        assert list(work_dir.iterdir()) == []

    asyncio.run(run())


def test_limited_local_python_ignores_nested_artifacts(tmp_path: Path):
    async def run():
        publisher = RecordingPublisher()
        tool = LimitedLocalPythonTool(
            LimitedLocalPythonConfig(
                work_dir=tmp_path / "runner",
                artifact_publisher=publisher,
                cleanup_run_dir=True,
            )
        )
        result = await tool.aexecute(
            "from pathlib import Path\n"
            "Path('nested').mkdir()\n"
            "Path('nested/hidden.png').write_bytes(b'not-public')\n"
            "Path('visible.png').write_bytes(b'public')"
        )

        payload = parsed(result)
        assert [item["name"] for item in payload["artifacts"]] == ["image-001.png"]
        assert publisher.published == [("visible.png", 1, "image/png")]

    asyncio.run(run())


def test_limited_local_python_limits_and_filters_artifacts(tmp_path: Path):
    async def run():
        publisher = RecordingPublisher()
        tool = LimitedLocalPythonTool(
            LimitedLocalPythonConfig(
                work_dir=tmp_path / "runner",
                artifact_publisher=publisher,
                max_artifact_files=2,
                cleanup_run_dir=True,
            )
        )
        result = await tool.aexecute(
            "from pathlib import Path\n"
            "Path('a.png').write_bytes(b'a')\n"
            "Path('b.svg').write_text('<svg/>')\n"
            "Path('c.jpg').write_bytes(b'c')\n"
            "Path('notes.txt').write_text('not an artifact')"
        )

        payload = parsed(result)
        assert len(payload["artifacts"]) == 2
        assert [item[0] for item in publisher.published] == ["a.png", "b.svg"]
        assert "notes.txt" not in result

    asyncio.run(run())


def test_limited_local_python_factory_names():
    tools, cleanup = create_limited_local_python_tools()

    assert [tool.metadata.name for tool in tools] == ["LimitedLocalPythonInterpreter"]
    assert "current working directory" in tools[0].metadata.description
    assert "Do not create date folders" in tools[0].metadata.description
    assert "Do not reproduce, rewrite, or invent artifact URLs" in tools[0].metadata.description
    assert callable(cleanup)


def test_limited_local_python_bundle():
    bundle = create_limited_local_python_bundle()

    assert [tool.metadata.name for tool in bundle.tools] == ["LimitedLocalPythonInterpreter"]
    assert len(bundle.cleanup_hooks) == 1
