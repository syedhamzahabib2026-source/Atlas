"""
Tool registry placeholder.

Future tools register here for orchestrator / agent dispatch.
"""

from __future__ import annotations

from typing import Any, Callable, Awaitable

# name -> async handler
ToolHandler = Callable[..., Awaitable[Any]]
_REGISTRY: dict[str, ToolHandler] = {}

# TODO: register Playwright tools for non-agent dispatch
# TODO: screenshot comparison tool
# TODO: vision analysis tool (GPT/Claude on PNG)


def register(name: str, handler: ToolHandler) -> None:
    _REGISTRY[name] = handler


def get(name: str) -> ToolHandler | None:
    return _REGISTRY.get(name)


def list_tools() -> list[str]:
    return list(_REGISTRY.keys())
