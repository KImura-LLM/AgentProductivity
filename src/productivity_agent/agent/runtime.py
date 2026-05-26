from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: ToolHandler


class AgentRuntime:
    """Small Hermes-style tool boundary for productivity operations."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, handler: ToolHandler) -> None:
        self._tools[name] = Tool(name=name, description=description, handler=handler)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    async def call(self, name: str, **kwargs: Any) -> Any:
        if name not in self._tools:
            raise KeyError(f"Unknown agent tool: {name}")
        return await self._tools[name].handler(**kwargs)
