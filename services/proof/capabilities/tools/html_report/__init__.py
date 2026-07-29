"""Proof-owned deterministic structured HTML report rendering.

The capability manifest is also imported by the standalone Proof service, where
the appliance tool runtime is intentionally not installed. Keep that dependency
lazy until Aituge actually builds the registered local tool.
"""

from typing import Any


def create_capability_html_report_bundle(config: Any):
    from .factory import create_capability_html_report_bundle as create_bundle

    return create_bundle(config)


__all__ = ["create_capability_html_report_bundle"]
