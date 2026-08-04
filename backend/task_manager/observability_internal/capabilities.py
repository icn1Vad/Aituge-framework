from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import InternalObservabilityError, invalid_request

HIGH_WATERMARK_RE = re.compile(r"^hwm_[A-Za-z0-9_-]{10,80}$")
CAPABILITY_MIN_LENGTH = 16
CAPABILITY_MAX_LENGTH = 4096
CAPABILITY_NAMES = frozenset(
    {
        "authorization",
        "x-observability-scope",
        "cursor",
        "retrytoken",
        "highwatermark",
        "accesscontext",
        "accesssession",
        "querysnapshotid",
        "locator",
        "downloadtoken",
        "traceparent",
    }
)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def new_high_watermark_handle() -> str:
    return "hwm_" + secrets.token_urlsafe(24)


def require_high_watermark_handle(value: str | None, request_id: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not HIGH_WATERMARK_RE.fullmatch(value):
        raise invalid_request(request_id, "OBSERVABILITY_HIGH_WATERMARK_INVALID")
    return value


@dataclass(frozen=True, slots=True)
class CapabilityClaims:
    kind: str
    handle: str
    position: int
    scope_hash: str
    filter_hash: str
    expires_at: int


class CapabilitySigner:
    """Sign opaque cursor/retry capabilities without logging their raw value."""

    def __init__(self, secret: bytes, *, issuer: str = "python-observability") -> None:
        if len(secret) < 32:
            raise ValueError(
                "Capability signing secret must contain at least 32 bytes."
            )
        self._secret = bytes(secret)
        self._issuer = issuer

    def issue(
        self,
        *,
        kind: str,
        handle: str,
        position: int,
        scope_hash: str,
        filter_hash: str,
        expires_at: int,
    ) -> str:
        if (
            kind not in {"cursor", "retry"}
            or not HIGH_WATERMARK_RE.fullmatch(handle)
            or isinstance(position, bool)
            or not isinstance(position, int)
            or position < 0
            or len(scope_hash) != 64
            or len(filter_hash) != 64
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
        ):
            raise ValueError("capability issuance facts are invalid")
        payload = {
            "iss": self._issuer,
            "typ": kind,
            "hwm": handle,
            "pos": position,
            "scp": scope_hash,
            "flt": filter_hash,
            "exp": expires_at,
            "nonce": secrets.token_urlsafe(12),
        }
        body = _b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
        )
        result = f"cap.{body}.{signature}"
        if not CAPABILITY_MIN_LENGTH <= len(result) <= CAPABILITY_MAX_LENGTH:
            raise ValueError("issued capability length is invalid")
        return result

    def verify(
        self,
        raw: str,
        *,
        expected_kind: str,
        expected_handle: str,
        expected_scope_hash: str,
        expected_filter_hash: str,
        request_id: str,
    ) -> CapabilityClaims:
        try:
            if (
                not isinstance(raw, str)
                or not CAPABILITY_MIN_LENGTH <= len(raw) <= CAPABILITY_MAX_LENGTH
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in raw
                )
            ):
                raise ValueError
            prefix, body, signature = raw.split(".", 2)
            if prefix != "cap":
                raise ValueError
            expected = hmac.new(
                self._secret, body.encode("ascii"), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(expected, _b64decode(signature)):
                raise ValueError
            payload = json.loads(_b64decode(body))
            if not isinstance(payload, Mapping):
                raise ValueError
            expires_at = _strict_int(payload.get("exp"))
            position = _strict_int(payload.get("pos"))
            claims = CapabilityClaims(
                kind=_strict_text(payload.get("typ"), maximum=16),
                handle=_strict_text(payload.get("hwm"), maximum=84),
                position=position,
                scope_hash=_strict_text(payload.get("scp"), maximum=64),
                filter_hash=_strict_text(payload.get("flt"), maximum=64),
                expires_at=expires_at,
            )
            if payload.get("iss") != self._issuer or expires_at <= int(time.time()):
                raise ValueError
            if claims.position < 0:
                raise ValueError
            if (
                claims.kind != expected_kind
                or claims.handle != expected_handle
                or claims.scope_hash != expected_scope_hash
                or claims.filter_hash != expected_filter_hash
            ):
                raise ValueError
            return claims
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            UnicodeError,
            binascii.Error,
            json.JSONDecodeError,
        ):
            raise invalid_request(
                request_id, "OBSERVABILITY_CAPABILITY_INVALID"
            ) from None


def reject_cursor_retry_conflict(
    cursor: str | None,
    retry_token: str | None,
    request_id: str,
) -> None:
    if cursor is not None and retry_token is not None:
        raise invalid_request(request_id, "OBSERVABILITY_PAGE_TOKEN_CONFLICT")
    for value in (cursor, retry_token):
        if (
            value is not None
            and not CAPABILITY_MIN_LENGTH <= len(value) <= CAPABILITY_MAX_LENGTH
        ):
            raise invalid_request(request_id, "OBSERVABILITY_CAPABILITY_INVALID")


def safe_capability_error(request_id: str) -> InternalObservabilityError:
    return invalid_request(request_id, "OBSERVABILITY_CAPABILITY_INVALID")


def contains_capability_name(value: str) -> bool:
    folded = value.casefold().replace("-", "").replace("_", "")
    return any(name.replace("-", "") in folded for name in CAPABILITY_NAMES)


def _strict_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError
    return value


def _strict_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError
    return value
