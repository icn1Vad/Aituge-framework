from __future__ import annotations

from backend import framework_runtime


def test_framework_runtime_starts_ten_workers_by_default(monkeypatch) -> None:
    monkeypatch.delenv("TASK_WORKER_COUNT", raising=False)

    commands = framework_runtime.runtime_commands()

    assert commands[0] == framework_runtime.API_COMMAND
    assert commands[1:] == (framework_runtime.WORKER_COMMAND,) * 10


def test_framework_runtime_clamps_configured_worker_count(monkeypatch) -> None:
    monkeypatch.setenv("TASK_WORKER_COUNT", "99")

    assert len(framework_runtime.runtime_commands()) == 33


def test_framework_runtime_supervises_api_and_worker(monkeypatch) -> None:
    monkeypatch.delenv("TASK_EXECUTION_MODE", raising=False)
    processes = []

    class FakeProcess:
        def __init__(self, command):
            self.command = list(command)
            self.poll_count = 0
            self.return_code = None
            self.terminated = False
            processes.append(self)

        def poll(self):
            self.poll_count += 1
            if self.command == ["api"] and self.poll_count >= 2:
                self.return_code = 0
            return self.return_code

        def terminate(self):
            self.terminated = True
            self.return_code = -15

        def wait(self, timeout=None):
            return self.return_code

        def kill(self):
            self.return_code = -9

    registered_signals = []
    monkeypatch.setattr(framework_runtime.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        framework_runtime.signal,
        "signal",
        lambda signum, callback: registered_signals.append((signum, callback)),
    )
    monkeypatch.setattr(framework_runtime.time, "sleep", lambda _seconds: None)

    return_code = framework_runtime.run_processes((("api",), ("worker",)))

    assert return_code == 1
    assert [process.command for process in processes] == [["api"], ["worker"]]
    assert processes[1].terminated is True
    assert {item[0] for item in registered_signals} == {
        framework_runtime.signal.SIGTERM,
        framework_runtime.signal.SIGINT,
    }
    assert framework_runtime.os.environ["TASK_EXECUTION_MODE"] == "worker"
