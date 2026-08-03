from __future__ import annotations

from aiohttp import web

from .app import create_app
from .settings import GatewaySettings


def main() -> None:
    settings = GatewaySettings.from_environment()
    web.run_app(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        access_log_format='%a %t "%r" %s %b "%{X-Request-ID}i"',
    )


if __name__ == "__main__":
    main()
