"""Speech-recognition provider adapters."""

from .aliyun_nls import AliyunNlsSpeechSession, nls_sdk_available, vendor_event

__all__ = ["AliyunNlsSpeechSession", "nls_sdk_available", "vendor_event"]
