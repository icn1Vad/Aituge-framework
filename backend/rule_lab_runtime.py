"""Dedicated lab credential: read the selected file, never fall back to old keys."""
import os
import re
from pathlib import Path


def read_key():
    path = os.getenv("RULE_LAB_KEY_FILE", "")
    if not path:
        raise ValueError("请配置测试台专用 DeepSeek key 文件")
    try:
        content = Path(path).read_text("utf-8-sig")
    except OSError as exc:
        raise ValueError("无法读取测试台专用 key 文件") from exc
    keys = re.findall(r"sk-[A-Za-z0-9_-]{20,}", content)
    if len(keys) != 1:
        raise ValueError("测试台专用文件必须包含且仅包含一个有效格式的 key")
    return keys[0]


def provider():
    from aituge_model.config import ModelRuntimeProvider, SecretResolver, load_model_registry
    # Pin the repository registration rather than inheriting a deployment gateway.
    registry = load_model_registry(str(Path(__file__).resolve().parents[1] / "aituge_model/config"))
    pack = registry.resolve_pack("api-rerank")
    if os.getenv("MODEL_GATEWAY_URL", "").strip():
        raise ValueError("测试台专用密钥不能经由旧模型网关，请清除本测试进程的网关配置")
    if os.getenv("RULE_LAB_MODEL_ID", "") != "deepseek-v4-flash":
        raise ValueError("测试台当前仅启用已验证的 deepseek-v4-flash")
    if pack.llm.base_url.rstrip("/") != "https://api.deepseek.com" or pack.llm.id != "deepseek-v4-flash":
        raise ValueError("测试台要求官方 DeepSeek 模型配置")
    return ModelRuntimeProvider(registry=registry, active_pack=pack,
        secret_resolver=SecretResolver(environment={}, overrides={pack.llm.credential_ref: read_key()}))


def live_status():
    if os.getenv("RULE_LAB_ENABLE_LIVE") != "1":
        return False, "真实模型未启用"
    try:
        provider()
        return True, "DeepSeek · 专用文件密钥"
    except ValueError as exc:
        return False, str(exc)


def build_runtime(tenant_id):
    if os.getenv("RULE_LAB_ENABLE_LIVE") != "1":
        raise ValueError("真实模型未启用")
    from service.conversation.llm_runner import LlmRuntime
    return LlmRuntime(tenant_id, model_runtime_provider=provider(), provider_max_retries=0)
