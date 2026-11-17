"""Synchronous access to the async FastMCP server for the agent loop.

The voice REPL and LangGraph nodes are synchronous; FastMCP is asyncio. A
single background event loop owns the server (and any proxied external MCP
sessions) and every call is submitted to it.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolOutcome:
    name: str
    text: str
    ok: bool
    seconds: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


class _Loop:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="openjarvis-mcp", daemon=True
        )
        self.thread.start()

    def run(self, coro: Any, timeout: Optional[float] = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


def slim_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Drop JSON-schema noise that costs prompt tokens on every request."""
    schema = copy.deepcopy(schema)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node.pop("additionalProperties", None)
            node.pop("title", None)
            any_of = node.get("anyOf")
            if isinstance(any_of, list):
                kept = [s for s in any_of if s.get("type") != "null"]
                if len(kept) == 1:
                    node.pop("anyOf")
                    node.update(kept[0])
            if node.get("default", "x") is None or node.get("default") == "":
                node.pop("default")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        return node

    return walk(schema)


_ERROR_PREFIX = re.compile(r"^Error calling tool '[^']+':\s*")


class ToolRuntime:
    """List and call the server's tools from synchronous code."""

    def __init__(
        self, server: Any, *, timeout: float = 90.0, hidden: Iterable[str] = ()
    ) -> None:
        self.server = server
        self.timeout = timeout
        # Mounted tools the model is never offered (nor can call), e.g. the
        # filesystem server's base64 media reader.
        self.hidden = set(hidden)
        self._loop = _Loop()
        self._tools: Dict[str, Any] = {}
        self._schemas: List[Dict[str, Any]] = []
        # A call whose caller was interrupted keeps running in the background;
        # the next call waits for it so two UI actions never overlap.
        self._busy: Optional[Any] = None

    def refresh(self) -> List[Dict[str, Any]]:
        tools = self._loop.run(self.server.list_tools(), timeout=60)
        tools = [t for t in tools if t.name not in self.hidden]
        self._tools = {t.name: t for t in tools}
        self._schemas = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": " ".join((t.description or "").split()),
                    "parameters": slim_schema(t.parameters or {"type": "object"}),
                },
            }
            for t in tools
        ]
        return self._schemas

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The event loop that owns the server (for calls from other code)."""
        return self._loop.loop

    @property
    def schemas(self) -> List[Dict[str, Any]]:
        if not self._schemas:
            self.refresh()
        return self._schemas

    @property
    def names(self) -> List[str]:
        if not self._tools:
            self.refresh()
        return list(self._tools)

    def annotations(self, name: str) -> Any:
        tool = self._tools.get(name)
        return getattr(tool, "annotations", None) if tool is not None else None

    def call(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> ToolOutcome:
        started = time.perf_counter()
        if self._tools and name not in self._tools:
            return ToolOutcome(name, f"There is no tool called {name}.", False)
        if self._busy is not None and not self._busy.done():
            try:
                self._busy.result(timeout=self.timeout)  # an interrupted call
            except Exception:
                pass
        future = asyncio.run_coroutine_threadsafe(
            self.server.call_tool(name, arguments or {}), self._loop.loop
        )
        self._busy = future
        try:
            result = future.result(timeout=self.timeout)
        except Exception as exc:  # ToolError, ValidationError, timeouts
            message = _ERROR_PREFIX.sub("", str(exc)).strip() or type(exc).__name__
            if "validation error" in message.lower():
                message = "Invalid arguments: " + " ".join(message.split())[:300]
            return ToolOutcome(name, message, False, time.perf_counter() - started)
        text = "\n".join(
            getattr(block, "text", "") for block in (result.content or [])
        ).strip()
        ok = not getattr(result, "is_error", False)
        return ToolOutcome(name, text or "Done.", ok, time.perf_counter() - started)

    def close(self) -> None:
        self._loop.stop()


__all__ = ["ToolOutcome", "ToolRuntime", "slim_schema"]
