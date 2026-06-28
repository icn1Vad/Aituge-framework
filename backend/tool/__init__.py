"""External tool layer for Aituge backend experiments.

This package owns concrete tool implementations and factories. The single
agent runtime should receive already-built FunctionTool instances from callers
instead of importing concrete tool clients directly.
"""

from .bundle import ToolBundle

__all__ = ["ToolBundle"]
