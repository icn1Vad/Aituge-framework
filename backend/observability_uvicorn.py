"""mTLS-only Uvicorn listener for internal observability APIs."""

from __future__ import annotations

import asyncio
import os
import re
import ssl
import stat
from pathlib import Path

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")


def _peer_common_name(transport: asyncio.Transport) -> str | None:
    ssl_object = transport.get_extra_info("ssl_object")
    if ssl_object is None:
        return None
    certificate = ssl_object.getpeercert()
    if not isinstance(certificate, dict):
        return None
    values: list[str] = []
    for relative_name in certificate.get("subject", ()):
        for key, value in relative_name:
            if key == "commonName" and isinstance(value, str):
                values.append(value)
    if len(values) != 1:
        return None
    identity = values[0]
    if _SAFE_IDENTITY.fullmatch(identity) is None:
        return None
    return identity


class MutualTlsH11Protocol(H11Protocol):
    """Inject a peer identity from the verified TLS transport into ASGI scope."""

    def connection_made(self, transport: asyncio.Transport) -> None:
        super().connection_made(transport)
        self._mtls_client_subject = _peer_common_name(transport)
        self._identity_scope: object | None = None

    def handle_events(self) -> None:
        super().handle_events()
        scope = self.scope
        if scope is None or scope is self._identity_scope:
            return
        self._identity_scope = scope
        if self._mtls_client_subject is not None:
            scope["mtls_client_subject"] = self._mtls_client_subject


def _validated_file(name: str, *, private: bool) -> str:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} is required")
    path = Path(raw)
    metadata = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_size < 1
        or metadata.st_size > 4 * 1024 * 1024
        or (private and stat.S_IMODE(metadata.st_mode) & 0o077)
    ):
        raise RuntimeError(f"{name} has unsafe metadata")
    return str(path)


def main() -> None:
    host = os.getenv("OBSERVABILITY_INTERNAL_BIND_HOST", "0.0.0.0").strip()
    port = int(os.getenv("OBSERVABILITY_INTERNAL_BIND_PORT", "9443"))
    if host not in {"0.0.0.0", "127.0.0.1"} or port < 1024 or port > 65535:
        raise RuntimeError("Internal observability bind address is invalid")
    config = uvicorn.Config(
        "backend.local_code_chat_app:create_app",
        factory=True,
        host=host,
        port=port,
        http=MutualTlsH11Protocol,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        ssl_certfile=_validated_file(
            "OBSERVABILITY_INTERNAL_MTLS_SERVER_CERT_FILE",
            private=False,
        ),
        ssl_keyfile=_validated_file(
            "OBSERVABILITY_INTERNAL_MTLS_SERVER_KEY_FILE",
            private=True,
        ),
        ssl_ca_certs=_validated_file(
            "OBSERVABILITY_INTERNAL_MTLS_CLIENT_CA_FILE",
            private=False,
        ),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
