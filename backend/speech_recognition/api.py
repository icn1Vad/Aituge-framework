"""FastAPI routes for the backend speech-recognition model capability."""

from __future__ import annotations

import asyncio
import json
import os
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aituge_model.config import ModelRuntimeProvider, ResolvedSpeechRecognitionModel

from .providers.aliyun_nls import AliyunNlsSpeechSession, nls_sdk_available
from .service import SessionFactory, SpeechSession, missing_configuration


_LEGACY_SECRET_ENV = {
    "aliyun_nls_appkey": "ALIYUN_NLS_APPKEY",
    "aliyun_nls_token": "ALIYUN_NLS_TOKEN",
    "aliyun_access_key_id": "ALIBABA_CLOUD_ACCESS_KEY_ID",
    "aliyun_access_key_secret": "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
}


def load_speech_recognition_model() -> ResolvedSpeechRecognitionModel:
    """Resolve ASR metadata and credentials through the shared model runtime."""
    overrides = {
        credential_ref: os.getenv(environment_name, "").strip()
        for credential_ref, environment_name in _LEGACY_SECRET_ENV.items()
        if os.getenv(environment_name, "").strip()
    }
    if "aliyun_access_key_id" not in overrides:
        legacy_id = os.getenv("ALIYUN_ACCESS_KEY_ID", "").strip()
        if legacy_id:
            overrides["aliyun_access_key_id"] = legacy_id
    if "aliyun_access_key_secret" not in overrides:
        legacy_secret = os.getenv("ALIYUN_ACCESS_KEY_SECRET", "").strip()
        if legacy_secret:
            overrides["aliyun_access_key_secret"] = legacy_secret
    runtime = ModelRuntimeProvider.from_environment(secret_overrides=overrides)
    return runtime.resolve_speech_recognition(require_credentials=False)


def create_speech_recognition_router(
    *,
    model: ResolvedSpeechRecognitionModel | None = None,
    session_factory: SessionFactory | None = None,
) -> APIRouter:
    effective_model = model or load_speech_recognition_model()
    effective_factory = session_factory or AliyunNlsSpeechSession
    sdk_required = session_factory is None
    router = APIRouter(tags=["model-capability:speech-recognition"])
    max_sessions = max(1, int(os.getenv("SPEECH_RECOGNITION_MAX_CONCURRENCY", "8")))
    session_slots = asyncio.Semaphore(max_sessions)

    @router.get("/api/speech/config")
    async def config_status():
        missing = missing_configuration(effective_model)
        sdk_ok = nls_sdk_available() if sdk_required else True
        return {
            "configured": not missing and sdk_ok,
            "credentials_configured": not missing,
            "sdk_available": sdk_ok,
            "model_id": effective_model.id,
            "provider": effective_model.provider,
            "missing": missing,
            "max_concurrent_sessions": max_sessions,
            "audio": {
                "format": f"{effective_model.audio_format}_s16le",
                "sample_rate": effective_model.sample_rate,
                "channels": effective_model.channels,
                "recommended_chunk_ms": effective_model.recommended_chunk_ms,
            },
        }

    @router.websocket("/ws/speech")
    async def speech_socket(websocket: WebSocket):
        await websocket.accept()
        missing = missing_configuration(effective_model)
        if missing:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "缺少语音识别模型凭证。",
                    "model_id": effective_model.id,
                    "missing": missing,
                }
            )
            await websocket.close(code=1011)
            return
        if sdk_required and not nls_sdk_available():
            await websocket.send_json(
                {"type": "error", "message": "未安装阿里云官方 Python NLS SDK。"}
            )
            await websocket.close(code=1011)
            return
        try:
            await asyncio.wait_for(session_slots.acquire(), timeout=0.1)
        except TimeoutError:
            await websocket.send_json({"type": "error", "message": "语音识别服务繁忙，请稍后重试。"})
            await websocket.close(code=1013)
            return


        loop = asyncio.get_running_loop()
        outbound: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        events_open = threading.Event()
        events_open.set()
        session: SpeechSession | None = None
        stopped_normally = False

        def emit(event: dict[str, object]) -> None:
            if not events_open.is_set():
                return
            try:
                loop.call_soon_threadsafe(outbound.put_nowait, event)
            except RuntimeError:
                pass

        async def send_events() -> None:
            while True:
                event = await outbound.get()
                if event is None:
                    return
                try:
                    await websocket.send_json(event)
                except (RuntimeError, WebSocketDisconnect):
                    return

        sender = asyncio.create_task(send_events())
        close_code = 1000
        try:
            session = effective_factory(effective_model, emit)
            await asyncio.to_thread(session.start)
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                audio = message.get("bytes")
                if audio is not None:
                    if not audio:
                        continue
                    if len(audio) % 2:
                        raise ValueError("PCM16 音频分片必须包含偶数字节。")
                    if len(audio) > 64 * 1024:
                        raise ValueError("单个音频分片不能超过 64 KiB。")
                    await asyncio.to_thread(session.send_audio, audio)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    command = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError("WebSocket 控制消息必须是 JSON。") from exc
                command_type = command.get("type") if isinstance(command, dict) else None
                if command_type == "stop":
                    await asyncio.to_thread(session.stop)
                    stopped_normally = True
                    break
                if command_type == "ping":
                    await outbound.put({"type": "pong"})
                    continue
                raise ValueError(f"不支持的控制消息：{command_type!r}")
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            close_code = 1011
            await outbound.put({"type": "error", "message": str(exc)})
        finally:
            if session is not None and not stopped_normally:
                try:
                    await asyncio.to_thread(session.shutdown)
                except Exception:
                    pass
            await asyncio.sleep(0)
            events_open.clear()
            await outbound.put(None)
            await sender
            session_slots.release()
            try:
                await websocket.close(code=close_code)
            except RuntimeError:
                pass

    return router
