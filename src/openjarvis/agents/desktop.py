"""``desktop`` agent: agentic control of this PC (LangGraph + FastMCP).

Used by ``jarvis listen``. Simple commands take the no-LLM fast path; the
rest run through a LangGraph loop over the FastMCP desktop tool server
(plus mounted MCP servers and bridged OpenJarvis tools), with long-term
memory, screen vision and spoken confirmation for risky actions.

Needs the ``agent`` extra: ``uv sync --extra agent``.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from openjarvis.agents._stubs import AgentContext, AgentResult, ToolUsingAgent
from openjarvis.core.registry import AgentRegistry
from openjarvis.core.types import Message, Role, ToolResult

logger = logging.getLogger(__name__)


def desktop_agent_available() -> bool:
    """True when the optional LangGraph + FastMCP dependencies are installed."""
    import importlib.util

    return all(
        importlib.util.find_spec(name) is not None for name in ("langgraph", "fastmcp")
    )


@AgentRegistry.register("desktop")
class DesktopAgent(ToolUsingAgent):
    """Agentic Windows assistant with tools, vision and memory."""

    agent_id = "desktop"
    accepts_tools = True
    _default_temperature = 0.3
    _default_max_tokens = 400

    def __init__(
        self,
        engine: Any,
        model: str,
        *,
        tools: Optional[List[Any]] = None,
        bus: Any = None,
        max_turns: Optional[int] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        loop_guard_config: Any = None,
        capability_policy: Any = None,
        agent_id: Optional[str] = None,
        rate_limiter: Any = None,
        interactive: bool = False,
        confirm_callback: Optional[Callable[[str], bool]] = None,
        skill_few_shot_examples: Optional[List[str]] = None,
        prompt_builder: Any = None,
        desktop_config: Any = None,
        memory_path: Optional[Path] = None,
        checkpoint_path: Optional[Path] = None,
        background_setup: bool = True,
    ) -> None:
        if desktop_config is None:
            try:
                from openjarvis.core.config import load_config

                desktop_config = load_config().desktop
            except Exception:
                from openjarvis.core.config import DesktopConfig

                desktop_config = DesktopConfig()
        self._cfg = desktop_config
        super().__init__(
            engine,
            model,
            tools=tools,
            bus=bus,
            max_turns=max_turns if max_turns is not None else self._cfg.max_steps,
            temperature=temperature
            if temperature is not None
            else self._cfg.temperature,
            max_tokens=max_tokens if max_tokens is not None else self._cfg.max_tokens,
            loop_guard_config=loop_guard_config,
            capability_policy=capability_policy,
            agent_id=agent_id,
            rate_limiter=rate_limiter,
            interactive=interactive,
            confirm_callback=confirm_callback,
            skill_few_shot_examples=skill_few_shot_examples,
            prompt_builder=prompt_builder,
        )
        self._confirm = confirm_callback
        self._progress: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self._memory_path = memory_path
        self._checkpoint_path = checkpoint_path
        self._ready = threading.Event()
        self._setup_error: Optional[BaseException] = None
        self._graph: Any = None
        self._runtime: Any = None
        self.memory: Any = None
        if background_setup:
            # Importing FastMCP/LangGraph takes a couple of seconds: overlap it
            # with wake-word and speech model loading.
            threading.Thread(
                target=self._setup, name="openjarvis-desktop-setup", daemon=True
            ).start()
        else:
            self._setup()

    # -- setup -------------------------------------------------------------------

    def _setup(self) -> None:
        try:
            from openjarvis.core.paths import get_config_dir
            from openjarvis.desktop import server as desktop_server
            from openjarvis.desktop.graph import DesktopGraph
            from openjarvis.desktop.memory import MemoryStore
            from openjarvis.desktop.runtime import ToolRuntime
            from openjarvis.desktop.winapi import ensure_dpi_aware

            ensure_dpi_aware()
            base = get_config_dir() / "desktop"
            memory_path = self._memory_path or (
                Path(self._cfg.memory_path)
                if self._cfg.memory_path
                else base / "memory.sqlite"
            )
            self.memory = MemoryStore(memory_path)
            desktop_server.CONTEXT.memory = self.memory
            desktop_server.CONTEXT.vision = (
                self._ask_vision if self._cfg.vision else None
            )
            desktop_server.CONTEXT.email_client = self._cfg.email_client
            desktop_server.CONTEXT.code_dir = getattr(self._cfg, "code_dir", "")
            desktop_server.CONTEXT.alarm_voice = self._cfg.alarm_voice
            desktop_server.CONTEXT.alarm_advance = self._cfg.alarm_advance_minutes
            searcher = next(
                (
                    t
                    for t in (self._tools or [])
                    if getattr(getattr(t, "spec", None), "name", "") == "web_search"
                ),
                None,
            )
            if searcher is not None:
                # open_page finds real page links with the same search backend.
                desktop_server.CONTEXT.link_search = lambda q, n: (
                    searcher.execute(query=q, max_results=n).content
                )

            from openjarvis.desktop import files_mcp, places
            from openjarvis.desktop.graph import SYSTEM_PROMPT

            server = desktop_server.build_server(
                external=self._external_servers(),
                files=getattr(self._cfg, "file_folders", "") or True,
            )
            if self._tools:
                desktop_server.add_openjarvis_tools(server, self._tools, self._executor)
            self._runtime = ToolRuntime(server, hidden=files_mcp.hidden_names())
            self._runtime.refresh()
            store = places.files()
            prompt = SYSTEM_PROMPT
            if store is not None:
                # open and the fast path look files up from this thread too.
                store.loop = self._runtime.loop
                prompt += (
                    "\n- Your folders (fs_* file tools take full paths inside "
                    "them): " + "; ".join(store.folders)
                )
            checkpoint = self._checkpoint_path or base / "threads.sqlite"
            self._graph = DesktopGraph(
                system_prompt=prompt,
                generate=self._generate_step,
                runtime=self._runtime,
                memory=self.memory,
                confirm=self._ask_confirm,
                max_steps=self._max_turns,
                history=self._cfg.history_messages,
                auto_finish=self._cfg.auto_finish,
                checkpoint_path=checkpoint,
                context_fn=self._live_context,
                on_event=self._on_event,
                recent=desktop_server.CONTEXT.recent,
                plan=getattr(self._cfg, "plan", False),
            )
        except BaseException as exc:  # surfaced on first use
            self._setup_error = exc
            logger.warning("Desktop agent setup failed: %s", exc)
        finally:
            self._ready.set()

    def _external_servers(self) -> List[Any]:
        specs: List[Any] = []
        if self._cfg.mcp_servers:
            specs.append(self._cfg.mcp_servers)
        try:
            from openjarvis.core.config import load_config

            mcp = load_config().tools.mcp
            if mcp.enabled and mcp.servers:
                specs.append(mcp.servers)
        except Exception:
            pass
        if not specs:
            return []
        from openjarvis.desktop.server import _external_servers

        merged: Dict[str, Any] = {}
        for spec in specs:
            merged.update(_external_servers(spec))
        return [{"name": name, **cfg} for name, cfg in merged.items()]

    def wait_ready(self, timeout: float = 120.0) -> None:
        self._ready.wait(timeout)
        if self._setup_error is not None:
            raise RuntimeError(
                f"Desktop agent unavailable: {self._setup_error}. Install it with "
                "`uv sync --extra agent`."
            ) from self._setup_error
        if self._graph is None:
            raise RuntimeError("Desktop agent is still starting up.")

    # -- hooks for the REPL ------------------------------------------------------

    def set_confirm(self, callback: Optional[Callable[[str], bool]]) -> None:
        """Replace how risky actions are confirmed (e.g. by voice)."""
        self._confirm = callback

    def set_progress(
        self, callback: Optional[Callable[[str, Dict[str, Any]], None]]
    ) -> None:
        """Receive ``(event, data)`` as the agent thinks and calls tools."""
        self._progress = callback

    def reset(self) -> None:
        if self._graph is not None:
            self._graph.reset()

    def _ask_confirm(self, question: str) -> bool:
        if self._confirm is None:
            return False
        return bool(self._confirm(question))

    def _on_event(self, kind: str, data: Dict[str, Any]) -> None:
        if self._progress is not None:
            self._progress(kind, data)

    # -- model calls -------------------------------------------------------------

    def _generate_step(
        self, messages: List[Message], tools: Any = None
    ) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        if tools:
            extra["tools"] = tools
        return self._generate(messages, **extra)

    def _engine_call(
        self,
        messages: List[Message],
        *,
        max_tokens: int,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """engine.generate with this session's num_ctx/num_gpu.

        Ollama reloads the model when num_ctx changes between requests, so
        warm-up and vision calls must use the same options as agent steps.
        """
        from openjarvis.agents._stubs import _ALLOWED_ENGINE_OPTION_KEYS

        options = {
            k: v
            for k, v in self._engine_options.items()
            if k in _ALLOWED_ENGINE_OPTION_KEYS
        }
        options.update(extra)
        return self._engine.generate(
            messages,
            model=model or self._model,
            temperature=self._temperature if temperature is None else temperature,
            max_tokens=max_tokens,
            **options,
        )

    def _ask_vision(self, prompt: str, image_b64: str, max_tokens: int) -> str:
        result = self._engine_call(
            [Message(role=Role.USER, content=prompt, images=[image_b64])],
            model=self._cfg.vision_model or self._model,
            temperature=0.1,
            max_tokens=max_tokens,
        )
        return str(result.get("content", "") or "")

    def _live_context(self) -> str:
        from openjarvis.desktop.winapi import app_window

        # Not the foreground window: that's usually Jarvis's own terminal.
        info = app_window()
        if info is None or not info.title:
            return ""
        return f"Active app: {info.title[:100]}"

    def _can_open(self, target: str) -> bool:
        from openjarvis.desktop import places
        from openjarvis.desktop.server import _recall_place

        try:
            from openjarvis.desktop.server import CONTEXT

            if places.best_listed(target, CONTEXT.recent.paths):
                return True  # a file from the folder just listed
            if places.scope_folder(target)[1] is not None:
                # "... in desktop": open decides (opens it, or asks which).
                return True
            found = places.resolve(target, recall_place=_recall_place)
        except Exception:
            return False
        return found is not None and found.confident

    # -- warm-up -----------------------------------------------------------------

    def _cloud_engine(self) -> bool:
        engine = self._engine
        for _ in range(3):  # through instrumentation wrappers
            if getattr(engine, "engine_id", "") == "groq":
                return True
            engine = getattr(engine, "_inner", None) or getattr(engine, "engine", None)
            if engine is None:
                break
        return False

    def warm_up(self) -> None:
        """Prime the prompt cache and the Windows app/file indexes."""
        self.wait_ready()

        def indexes() -> None:
            from openjarvis.desktop import places

            try:
                store = places.files()
                if store is not None:
                    store.warm()  # first look at Desktop/Documents/Downloads
                from openjarvis.pc.control import find_app

                find_app("openjarvis warm-up")  # loads the Start Menu app list
            except Exception as exc:
                logger.debug("Index warm-up failed: %s", exc)

        worker = threading.Thread(target=indexes, name="openjarvis-index-warmup")
        worker.start()
        if self._cloud_engine():
            # Nothing to load on a hosted model, and the prompt + tool list
            # would use up part of the per-minute token allowance.
            worker.join(timeout=30)
            return
        from openjarvis.desktop.graph import to_messages

        try:
            self._engine_call(
                to_messages(
                    [
                        {"role": "system", "content": self._graph.system_prompt},
                        {"role": "user", "content": "Hello"},
                    ]
                ),
                tools=self._runtime.schemas,
                max_tokens=1,
            )
        except Exception as exc:
            logger.debug("Prompt-cache warm-up failed: %s", exc)
        worker.join(timeout=30)

    # -- running -----------------------------------------------------------------

    def run(
        self, input: str, context: Optional[AgentContext] = None, **kwargs: Any
    ) -> AgentResult:
        self.wait_ready()
        self._emit_turn_start(input)
        request = " ".join((input or "").split())
        started = time.perf_counter()
        try:
            from openjarvis.desktop.server import CONTEXT as _context

            _context.request = request  # tools read the user's own words
        except Exception:
            pass

        intent = None
        if self._cfg.fast_path:
            from openjarvis.desktop import fastpath

            try:
                from openjarvis.desktop.server import CONTEXT

                intent = fastpath.match(
                    request,
                    can_open=self._can_open,
                    tools=set(self._runtime.names),
                    recent=CONTEXT.recent,
                )
            except Exception:
                logger.debug("fast path failed", exc_info=True)
        if intent is not None:
            text, ok = self._graph.run_tool(intent.tool, intent.args)
            declined = text.startswith("The user did not allow")
            from openjarvis.desktop.graph import _ASKS_USER

            # "Did you mean A or B? Say which." is for you, not the model.
            asks_you = not ok and bool(_ASKS_USER.search(text))
            if ok or declined or asks_you or not intent.fallback_on_error:
                reply = "Okay, I won't." if declined else text
                self._graph.record(request, intent.tool, intent.args, reply, ok)
                self._emit_turn_end(turns=0)
                return AgentResult(
                    content=reply,
                    tool_results=[
                        ToolResult(tool_name=intent.tool, content=text, success=ok)
                    ],
                    turns=0,
                    metadata={
                        "fast_path": True,
                        "seconds": time.perf_counter() - started,
                    },
                )

        state = self._graph.invoke(request)
        reply = state.get("reply") or "Done."
        log = state.get("tool_log") or []
        self._emit_turn_end(turns=state.get("steps", 0))
        return AgentResult(
            content=_spoken(reply),
            tool_results=[
                ToolResult(tool_name=e["name"], content=e["text"], success=e["ok"])
                for e in log
            ],
            turns=int(state.get("steps", 0)),
            metadata={"fast_path": False, "seconds": time.perf_counter() - started},
        )


def _spoken(text: str) -> str:
    """The reply as shown on screen (Markdown kept; speech cleans it up)."""
    return (text or "").strip() or "Done."


__all__ = ["DesktopAgent", "desktop_agent_available"]
