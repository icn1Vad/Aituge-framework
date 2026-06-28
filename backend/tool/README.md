# Backend Tool Layer

`backend/tool` is the external tool implementation layer.

The intended boundary is:

- `backend/tool` owns concrete clients, adapters, configs, and factories.
- `backend/single-agent` receives ready-to-use `FunctionTool` instances.
- Task/API layers decide which tools are enabled for a run.

The first sandbox implementation follows PAI-RAG's simpler shape: create a
remote code sandbox client, wrap it as a small set of LlamaIndex tools, and
return a cleanup hook for the caller to run after the conversation/task.

The initial sandbox is intentionally named `limited` because it is a bounded
execution helper, not a general-purpose secure isolation product.

`backend/tool/local_runtime` also provides a minimal Codex-like local Python
runner. It executes code on the host with a temporary working directory,
timeout, output truncation, and cleanup. It is built for trusted local testing,
not hardened isolation.

## Task-Owned Injection

The single-agent runtime should not import concrete tool implementations. The
task/API layer owns tool selection and passes tools into the runner:

```python
from service.agent import SingleAgentRunner
from tool.sandbox import LimitedCodeSandboxConfig, create_limited_code_sandbox_bundle

bundle = create_limited_code_sandbox_bundle(
    LimitedCodeSandboxConfig(
        enabled=True,
        aliyun_id="...",
        interpreter_name="...",
        api_key="...",
    )
)

try:
    result = await SingleAgentRunner().chat(
        messages=[{"role": "user", "content": "run a quick calculation"}],
        tools=bundle.tools,
    )
finally:
    await bundle.cleanup()
```

For HTTP tests or task adapters, pass a provider into the app/router:

```python
app = create_app(tool_provider=lambda request: bundle)
```

For a local-code test app:

```bash
uvicorn backend.local_code_chat_app:create_app --factory --host 0.0.0.0 --port 8892
```
