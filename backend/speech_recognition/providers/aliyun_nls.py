"""Alibaba Cloud NLS provider adapter."""

import importlib.util
import json

from aituge_model.config import ResolvedSpeechRecognitionModel

from ..service import EventSink


def nls_sdk_available() -> bool:
    return importlib.util.find_spec("nls") is not None


def vendor_event(message: str | bytes, event_type: str) -> dict[str, object]:
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    try:
        decoded = json.loads(message)
    except (TypeError, json.JSONDecodeError):
        return {"type": event_type, "text": "", "vendor_message": str(message)}

    header = decoded.get("header") if isinstance(decoded, dict) else {}
    payload = decoded.get("payload") if isinstance(decoded, dict) else {}
    if not isinstance(header, dict):
        header = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "type": event_type,
        "text": str(payload.get("result") or payload.get("text") or ""),
        "vendor_event": str(header.get("name") or ""),
        "status": header.get("status"),
        "status_text": str(header.get("status_text") or ""),
    }


class AliyunNlsSpeechSession:
    """One streaming inference session using Alibaba Cloud's official SDK."""

    def __init__(self, model: ResolvedSpeechRecognitionModel, emit: EventSink):
        if model.provider != "aliyun_nls":
            raise ValueError(f"Unsupported speech recognition provider: {model.provider}")
        self._model = model
        self._emit = emit
        self._transcriber = None

    def _resolve_token(self) -> str:
        if self._model.token:
            return self._model.token
        from nls.token import getToken

        return getToken(
            self._model.access_key_id,
            self._model.access_key_secret,
            domain=self._model.region,
            url=self._model.meta_endpoint,
        )

    def start(self) -> None:
        import nls

        self._transcriber = nls.NlsSpeechTranscriber(
            url=self._model.base_url,
            token=self._resolve_token(),
            appkey=self._model.app_key,
            on_start=lambda message, *_: self._emit(vendor_event(message, "ready")),
            on_sentence_begin=lambda message, *_: self._emit(
                vendor_event(message, "sentence_begin")
            ),
            on_result_changed=lambda message, *_: self._emit(
                vendor_event(message, "partial")
            ),
            on_sentence_end=lambda message, *_: self._emit(
                vendor_event(message, "final")
            ),
            on_completed=lambda message, *_: self._emit(
                vendor_event(message, "completed")
            ),
            on_error=lambda message, *_: self._emit(vendor_event(message, "error")),
            on_close=lambda *_: self._emit({"type": "connection_closed"}),
        )
        self._transcriber.start(
            aformat=self._model.audio_format,
            sample_rate=self._model.sample_rate,
            enable_intermediate_result=True,
            enable_punctuation_prediction=True,
            enable_inverse_text_normalization=True,
        )

    def send_audio(self, data: bytes) -> None:
        if self._transcriber is None:
            raise RuntimeError("NLS session has not started")
        self._transcriber.send_audio(data)

    def stop(self) -> None:
        if self._transcriber is not None:
            self._transcriber.stop()

    def shutdown(self) -> None:
        if self._transcriber is not None:
            self._transcriber.shutdown()
