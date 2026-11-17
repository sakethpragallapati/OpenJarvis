"""The desktop agent loop as a LangGraph state graph.

    START -> prepare -> think --(tool calls)--> act --(more to do)--> think
                          |                      |
                          +------(answer)------> finish <-(auto-finish)-+

- **prepare** adds the request and recalls relevant long-term memories.
- **think** calls the model with the tool list (prompt prefix kept stable
  so Ollama's KV cache makes repeat calls cheap).
- **act** runs tool calls through the FastMCP runtime, asking the user
  first for anything risky. A single-step request whose tool already
  produced the answer ("Opened Task Manager.") finishes here without a
  second model call.
- **finish** writes the reply and logs the episode to memory.

The conversation (with tool calls and results) is checkpointed in SQLite
per day, so follow-ups ("now open the second one") have context and a
restart doesn't forget the day's work.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Optional, TypedDict

from openjarvis.core.types import Message, Role, ToolCall

logger = logging.getLogger(__name__)

MAX_HISTORY = 60

SYSTEM_PROMPT = "\n".join(
    [
        "You are Jarvis, a fast voice assistant that controls this Windows "
        "laptop through tools.",
        "- To do anything, call a tool. Never claim an action you did not do "
        "with a tool, and never say something worked unless a tool result "
        "shows it (a saved file name, the output, 'Sent to ...').",
        "- Follow-ups refer to the conversation and the [Recent] note: 'it', "
        "'him', 'that chat', 'the next one', 'now compare it with X' mean "
        "the last thing you worked on. If your previous step failed and the "
        "user now gives a name or a correction, retry that step with it.",
        "- Several parts in one request: do every part in order, one tool "
        "step at a time, checking each result; you're done only when all "
        "parts are done.",
        "- Apps, folders, files, websites: open (names may be partial or "
        "misheard; open finds them). A file from a list you just showed: "
        "open with its name. Never ui/mouse/screen to open a file.",
        "- Files and folders: the fs_ tools with full paths - fs_list_directory "
        "(what's in a folder), fs_search_files (find by name: pattern "
        "'**/*word*'), fs_read_text_file (read or summarise, PDF and Office "
        "too), fs_write_file (new file), fs_edit_file / fs_append_file "
        "(change one), fs_create_directory, fs_move_file (move or rename), "
        "fs_copy_file, fs_get_file_info. Delete: recycle. Never run_command "
        "for files.",
        "- Songs and music (any song, 'X songs', playlists): play_youtube with "
        "where youtube_music, always. Videos (reviews, trailers): play_youtube "
        "where youtube. Never browser_search or mouse for these. 'The next "
        "one' = which 0; 'open the first video' = which 1; 'the third video "
        "on the right side / in the suggestions / on the page' = which 3 with "
        "on_page true. Resume/pause: system play_pause.",
        "- Web: decide what the user wants. 'Open the Amazon page/website of "
        "X', 'open X on Flipkart', 'open the first 3 websites/results' means "
        "the real pages: open_page (site, count; no query = the last search's "
        "results). 'Search X on <site>' means that site's search page: one "
        "browser_search. A question ('what', 'which', 'best', 'how much', "
        "'compare', 'check', 'suggest', 'tell me') means answer it: "
        "web_search (one per thing, at most 3) and answer from the results; "
        "don't also open the browser. Never state a price, score or date "
        "from memory.",
        "- Calls: whatsapp call / video_call (it asks first). Never dial a "
        "phone number or invent one.",
        "- Writing a program or script: code write with the complete code "
        "(it saves a file and opens VS Code); running it: code run, which "
        "returns the real output. Never type code with keyboard.",
        "- Inside an app: ui lists/clicks/types into the active window's "
        "buttons, links and fields; keyboard types text and presses shortcuts "
        "like ctrl+t or enter; screen describes what is visible or clicks "
        "something by description. To verify a step, screen check with a "
        "yes/no question. Never click the mouse at guessed x/y.",
        "- Tool results say which window is in front afterwards. If a dialog "
        "you didn't expect opened (e.g. 'Save As'), handle it or tell the user.",
        "- Do only what was asked. Never substitute a different action for one "
        "you can't do (no phone calls through WhatsApp Web, so say that). As "
        "soon as it is done, stop calling tools and reply.",
        "- To press a key (delete, enter, ctrl+s) use keyboard hotkey; never "
        "type a key's name as text. Never select-all and overwrite text "
        "unless the user asked to replace it.",
        "- Alarms, timers and reminders: schedule (they are real Windows alarms).",
        "- WhatsApp: whatsapp open_chat / send / read / unread / list (never "
        "ui or keyboard for WhatsApp). If a chat isn't found, tell the user "
        "the suggested names and let them choose; never open one yourself or "
        "open the contacts list unless asked.",
        "- Email: email compose with to, subject and the full body you write; "
        "email send only if asked to send. Never type an email with keyboard.",
        "- Questions about the screen: screen describe with the user's "
        "question as query.",
        "- When the user tells you something to remember, use memory remember; "
        "use memory recall for questions about the past.",
        "- run_command (PowerShell) only when no other tool fits.",
        "- Reply in one or two short spoken sentences. Only when the user asks "
        "for details, a report, a list or a comparison: a one-line summary "
        "first, then up to 8 short Markdown bullet points (no tables, no "
        "headings). If something failed or you couldn't check it, say so "
        "plainly.",
    ]
)

PLAN_PROMPT = (
    "You plan tool steps for a voice assistant that controls a Windows PC. "
    "Split the user's request into the fewest concrete steps (at most 6), one "
    "per line, numbered, each naming the tool to use ({tools}). Resolve "
    "'it'/'him'/'the next one' using the context. Add a last step to check "
    "the result if it can be checked. Write only the numbered steps."
)

_MULTI_STEP = re.compile(
    r"\b(and|then|after that|afterwards|also|plus|next|followed by)\b|[,;]", re.I
)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_TEXT_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def _append_capped(
    left: List[Dict[str, Any]], right: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    merged = list(left or []) + list(right or [])
    if len(merged) <= MAX_HISTORY:
        return merged
    trimmed = merged[-MAX_HISTORY:]
    # Never start on a tool result / tool call: the model needs the pair.
    for i, message in enumerate(trimmed):
        if message.get("role") == "user":
            return trimmed[i:]
    return trimmed


class AgentState(TypedDict, total=False):
    messages: Annotated[List[Dict[str, Any]], _append_capped]
    request: str
    note: str
    steps: int
    reply: str
    done: bool
    blocked: int
    tool_log: List[Dict[str, Any]]
    checked: bool  # the reply's numbers were already checked once this turn


_FACTS = re.compile(
    r"\b(?:price|prices|stock|stocks|share|shares|compare|comparison|weather|"
    r"temperature|score|scores|news|exchange rate|rate|how much|who won|latest|"
    r"population|market cap|worth|cost|"
    # "suggest which one to buy" once quoted a price no search result had.
    r"suggest|suggestions?|recommend\w*|buy|best|specs?|specifications|cheapest|"
    r"under \d[\d,]*)\b",
    re.I,
)
# Numbers worth checking: 2+ digits, decimals, money, percentages.
_NUMBER = re.compile(r"(?<![\w.])[$₹€£]?\d[\d,]*(?:\.\d+)?%?(?![\w])")


def ungrounded_numbers(reply: str, sources: List[str]) -> List[str]:
    """Numbers in ``reply`` that appear in none of ``sources``."""
    haystack = " ".join(sources).replace(",", "")
    missing = []
    for match in _NUMBER.finditer(reply or ""):
        token = match.group(0)
        digits = token.strip("$₹€£%").replace(",", "")
        if len(digits.replace(".", "")) < 2:
            continue  # "1", "2": counting, not facts
        if digits not in haystack and digits.rstrip("0").rstrip(".") not in haystack:
            missing.append(token)
    return missing


def to_messages(dicts: List[Dict[str, Any]]) -> List[Message]:
    out = []
    for d in dicts:
        calls = [
            ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments") or "{}")
            for c in d.get("tool_calls") or []
        ]
        out.append(
            Message(
                role=Role(d["role"]),
                content=d.get("content") or "",
                tool_calls=calls or None,
                tool_call_id=d.get("tool_call_id"),
                name=d.get("name"),
            )
        )
    return out


def window(messages: List[Dict[str, Any]], size: int) -> List[Dict[str, Any]]:
    """The last ``size`` messages, starting at a user turn, old results cut."""
    recent = list(messages[-size:])
    for i, message in enumerate(recent):
        if message.get("role") == "user":
            recent = recent[i:]
            break
    last_user = max(
        (i for i, m in enumerate(recent) if m.get("role") == "user"), default=0
    )
    shrunk = []
    for i, message in enumerate(recent):
        content = message.get("content") or ""
        if i < last_user and message.get("role") == "tool" and len(content) > 400:
            message = {**message, "content": content[:400] + " [...]"}
        shrunk.append(message)
    return shrunk


def clean_reply(text: str) -> str:
    """The model's answer without think blocks or text tool calls.

    Line breaks and Markdown stay: the console shows the reply formatted,
    and the voice path rewrites it for speech separately.
    """
    text = _THINK.sub("", text or "")
    text = _TEXT_TOOL_CALL.sub("", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def clean_reply_lines(text: str) -> List[str]:
    """Model text without think blocks or Markdown, keeping line breaks."""
    text = _THINK.sub("", text or "")
    text = _TEXT_TOOL_CALL.sub("", text)
    # Underscores stay: plan lines name tools like web_search.
    return [re.sub(r"[*`#>]+", "", line).strip() for line in text.splitlines()]


def parse_text_tool_calls(text: str) -> List[Dict[str, Any]]:
    """Qwen sometimes writes ``<tool_call>{...}</tool_call>`` as plain text."""
    calls = []
    for match in _TEXT_TOOL_CALL.finditer(text or ""):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("name"):
            args = data.get("arguments", {})
            calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "name": str(data["name"]),
                    "arguments": json.dumps(args)
                    if not isinstance(args, str)
                    else args,
                }
            )
    return calls


def is_single_step(request: str) -> bool:
    # "Jarvis, open YouTube" is one step: drop the name and politeness first.
    from openjarvis.desktop.fastpath import normalize

    text = normalize(request or "")
    return not (_MULTI_STEP.search(text) or _NEEDS_SEVERAL.search(text))


# One sentence, several lookups: "compare it with Google's stock price".
_NEEDS_SEVERAL = re.compile(r"\b(?:compare|comparison|versus|vs|both|each)\b", re.I)


# Calls that legitimately repeat back-to-back (paging, pressing keys).
_REPEATABLE = {("keyboard", "hotkey"), ("mouse", "scroll_up"), ("mouse", "scroll_down")}


def _same_call(previous: Dict[str, Any], name: str, args: Dict[str, Any]) -> bool:
    if (name, str(args.get("action", ""))) in _REPEATABLE:
        return False
    return previous.get("name") == name and previous.get("args") == args


_TOOL_HINTS = (
    # Writing mail; "check my gmail" is just opening Gmail.
    (re.compile(r"\b(?:e-?mails?|mail to)\b", re.I), "email"),
    (re.compile(r"\bwhats\s?app\b", re.I), "whatsapp"),
    (re.compile(r"\b(?:alarms?|timers?|remind(?:er)?s?)\b", re.I), "schedule"),
    (
        re.compile(
            r"\b(?:program|script|code it|hello world|python|javascript|"
            r"run (?:it|the (?:code|program|script))|execute|the output)\b",
            re.I,
        ),
        "code",
    ),
    (
        re.compile(
            r"\b(?:open|show|go to|visit)\b.*\b(?:websites?|web ?pages?|pages?|"
            r"links?|results?|product)\b|\bfirst (?:\w+ )?(?:websites|links|results)\b",
            re.I,
        ),
        "open_page",
    ),
    (
        re.compile(
            r"\b(?:price|prices|stock|stocks|compare|comparison|weather|score|"
            r"news|exchange rate|how much is|who won|latest)\b",
            re.I,
        ),
        "web_search",
    ),
    (
        re.compile(r"\b(?:my screen|on (?:the )?screen|see (?:my|the) screen)\b", re.I),
        "screen",
    ),
)


def tool_hint(request: str, available: Optional[List[str]] = None) -> str:
    """Name the tool when the request names the channel ("email my friend").

    The 4B model sometimes picks a neighbouring tool (WhatsApp for an
    email); naming it costs a few tokens and fixes that.
    """
    tools = [tool for pattern, tool in _TOOL_HINTS if pattern.search(request or "")]
    if available is not None:
        tools = [
            t
            for t in tools
            if t in ("email",) or t in available or t.split(" ")[0] in available
        ]
    if available and "gmail_send" in available:
        # With Gmail connected, mail goes through the Gmail MCP tools.
        tools = [
            "gmail_draft / gmail_send / gmail_unread / gmail_search"
            if t == "email"
            else t
            for t in tools
        ]
    return f"Use the {' / '.join(tools)} tool." if tools else ""


def with_defaults(name: str, args: Dict[str, Any], request: str) -> Dict[str, Any]:
    """Fill arguments small models forget.

    ``screen describe`` without a question made the vision model give the
    same generic description every time; the user's own words are the
    question.
    """
    action = args.get("action")
    if isinstance(action, str):
        # "hotkey>", " Type", "open-chat": the 4B model's near-misses.
        clean = re.sub(r"[^a-z_]", "", action.strip().lower().replace("-", "_"))
        if clean != action:
            args = {**args, "action": clean}
    if (
        name == "screen"
        and args.get("action") in ("describe", "read", "check")
        and not str(args.get("query", "")).strip()
    ):
        return {**args, "query": request}
    return args


# Disruptive actions the model must not pick unless the user asked for them
# ("make a call to me" once became "lock the screen").
_ASKED_FOR = {
    ("system", "lock"): r"\block",
    ("system", "shutdown"): r"shut\s*(?:it\s+)?down|power off|turn off the "
    r"(?:pc|computer|laptop)",
    ("system", "restart"): r"restart|reboot",
    ("system", "sleep"): r"\bsleep\b|hibernate",
    ("system", "sign_out"): r"sign (?:me )?out|log ?(?:me )?(?:off|out)",
    ("system", "empty_recycle_bin"): r"recycle bin|trash",
    ("window", "close"): r"\bclose\b|\bquit\b|\bexit\b|\bkill\b|\bend\b|"
    r"\bshut\b",
    ("process", "kill"): r"\bkill\b|\bend\b|\bforce\b|\bclose\b|\bstop\b|"
    r"\bquit\b|not responding|frozen|stuck",
    # The model once "fixed" a misheard name by saving the nickname on = on.
    ("contacts", "add"): r"\bcontacts?\b|\bnick ?names?\b|when i say|if i say|"
    r"\bi mean\b|\bsave\b.*\bas\b|\badd\b.*\bas\b",
    ("contacts", "forget"): r"\bforget\b|\bremove\b|\bdelete\b",
    # After "Ammatak" wasn't found, the model opened contacts.txt in Notepad.
    ("contacts", "open"): r"\bcontacts?\b|\bnick ?names?\b",
    # "Search for phones on Amazon" once also opened five product tabs. A
    # yes to "Shall I open the pages?" counts as asking.
    ("open_page", ""): r"\b(?:open|show|visit|go to|take me|bring up|pull up|"
    r"websites?|web ?pages?|pages?|links?|tabs?|yes|yeah|yep|sure|ok(?:ay)?|"
    r"please do|go ahead|do it)\b",
}
_ASKED_LABEL = {"open_page": "open web pages"}


# A tool result that is a question for the user, not for the model.
_ASKS_USER = re.compile(r"\bSay which\b|\bwhich one do you mean\b", re.I)

# Most calls of one tool in a single request. "Check phones under 30,000 on
# Amazon" once alternated browser_search and web_search eight times (43 s).
_PER_REQUEST = {
    "web_search": 3,
    "browser_search": 2,
    "open_page": 2,
    "play_youtube": 2,
    "mouse": 3,
    "open": 3,
    "fs_search_files": 3,
    "screen": 4,
    "whatsapp": 4,
    "contacts": 2,
    "run_command": 3,
}


_CLICKS = {"click", "double_click", "right_click"}
_SAYS_WHERE = re.compile(
    r"\b(?:mouse|coordinates?|pixels?|cursor|middle of the screen|centre of the "
    r"screen|center of the screen|top (?:left|right)|bottom (?:left|right))\b",
    re.I,
)


_FILE_NAME = re.compile(
    r"\.(?:pdf|docx?|xlsx?|pptx?|txt|md|csv|png|jpe?g|gif|mp[34]|mkv|zip|rar|"
    r"py|js|html?|exe|lnk)\b",
    re.I,
)


def file_by_clicking(name: str, args: Dict[str, Any]) -> Optional[str]:
    """Clicking a file's name to open it (the model tried "ui click CC Mid-1
    Notes.docx.pdf" in Jarvis's own window, again and again)."""
    if name not in ("ui", "screen") or "click" not in str(args.get("action", "")):
        return None
    what = str(args.get("name") or args.get("query") or "")
    if not _FILE_NAME.search(what):
        return None
    return (
        "Not done: to open a file, call open with its name or full path "
        f"(open target={what!r}); don't click it."
    )


def blind_click(
    name: str, args: Dict[str, Any], log: List[Dict[str, Any]], request: str
) -> Optional[str]:
    """A mouse click at made-up coordinates ("open the first video" once
    clicked (500, 300) twice and hit nothing). Clicks are fine after finding
    the spot (screen locate / ui list this request) or when the user said
    where. ``screen describe`` doesn't count: it gives no positions, and the
    model clicked guesses after it four times, pausing the video."""
    if name != "mouse" or str(args.get("action", "")) not in _CLICKS:
        return None
    if any(
        e.get("ok")
        and (e.get("name"), str((e.get("args") or {}).get("action", "")))
        in (("screen", "locate"), ("ui", "list"))
        for e in log
    ):
        return None
    if _SAYS_WHERE.search(request or ""):
        return None
    return (
        "Not done: that would click a guessed spot. Click by name with ui click, "
        "or by description with screen click; for a video listed on the page "
        "use play_youtube with which and on_page true."
    )


def over_limit(name: str, log: List[Dict[str, Any]]) -> Optional[str]:
    """Why another ``name`` call this request is one too many (None = fine)."""
    limit = _PER_REQUEST.get(name)
    if limit is None:
        return None
    used = sum(1 for entry in log if entry.get("name") == name)
    if used < limit:
        return None
    return (
        f"Not done: {name} was already used {used} times for this request. "
        "Answer the user now with what you have."
    )


def fit_choices(
    name: str, args: Dict[str, Any], schemas: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Map an argument outside a tool's allowed values to the closest one
    (``kind='apps'`` for open -> 'app', ``kind='everything'`` -> dropped;
    ``action='open-chat'`` -> 'open_chat') instead of failing the step."""
    import difflib

    schema = next(
        (
            s.get("function", {}).get("parameters", {})
            for s in schemas or []
            if s.get("function", {}).get("name") == name
        ),
        None,
    )
    if not schema:
        return args
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fixed = dict(args)
    for key, value in args.items():
        choices = properties.get(key, {}).get("enum")
        if not choices or not isinstance(value, str) or value in choices:
            continue
        close = difflib.get_close_matches(value.lower(), choices, n=1, cutoff=0.6)
        if close:
            fixed[key] = close[0]
        elif key not in required:
            fixed.pop(key)  # the tool's default applies
    return fixed


def unrequested(name: str, args: Dict[str, Any], request: str) -> Optional[str]:
    """Why this call wasn't asked for (None = fine)."""
    action = str(args.get("action", ""))
    pattern = _ASKED_FOR.get((name, action))
    if pattern is None or re.search(pattern, request or "", re.I):
        return None
    what = action.replace("_", " ") if action else _ASKED_LABEL.get(name, name)
    return (
        f"Not done: the user didn't ask to {what}. If you can't do what they "
        "asked, tell them so instead."
    )


_ADMITS = re.compile(
    r"\b(?:couldn'?t|can'?t|cannot|didn'?t|did not|failed|unable|not able|"
    r"wasn'?t|isn'?t|won'?t|not (?:sure|found|done|sent|saved)|sorry|"
    r"no (?:chat|match|such)|okay,? i won'?t|instead)\b",
    re.I,
)


# Jarvis's own refusals of a call ("Not done: browser_search was already used
# 2 times...") are notes for the model, never something to tell the user.
_GUARD_NOTE = re.compile(r"^(?:Not done:|Not repeated:)")


def _real_steps(log: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in log if not _GUARD_NOTE.match(str(e.get("text", "")))]


def honest_reply(reply: str, log: List[Dict[str, Any]]) -> str:
    """Don't let a reply claim success when the last step failed or the user
    said no ("I've already sent it" after a declined send)."""
    if not log:
        return reply
    real = _real_steps(log)
    if not real:
        # Every call was refused, so nothing happened: never let "Done, I've
        # called them" stand, and never read the internal note out.
        return reply if _ADMITS.search(reply or "") else "I couldn't do that."
    log = real
    if log[-1].get("ok"):
        return reply
    last = log[-1]
    text = str(last.get("text", ""))
    if text.startswith("The user did not allow"):
        return reply if _ADMITS.search(reply or "") else "Okay, I didn't do that."
    if _ADMITS.search(reply or "") or _ASKS_USER.search(reply or ""):
        return reply  # an admission, or a question back ("Say which.")
    return "I couldn't finish that. " + text[:200]


def _fallback_reply(log: List[Dict[str, Any]]) -> str:
    """Something useful to say when the model ended without words."""
    if log and not _real_steps(log):
        return "I couldn't do that."
    log = _real_steps(log)
    for entry in reversed(log):
        if entry.get("ok") and len(entry.get("text", "")) <= 200:
            return entry["text"]
    failed = next((e for e in reversed(log) if not e.get("ok")), None)
    if failed is not None:
        return "I couldn't finish that. " + failed.get("text", "")[:160]
    return "Done."


class DesktopGraph:
    """Builds and runs the LangGraph agent."""

    def __init__(
        self,
        *,
        generate: Callable[..., Dict[str, Any]],
        runtime: Any,
        memory: Any = None,
        confirm: Optional[Callable[[str], bool]] = None,
        system_prompt: str = SYSTEM_PROMPT,
        max_steps: int = 8,
        history: int = 24,
        auto_finish: bool = True,
        checkpoint_path: Optional[Path] = None,
        context_fn: Optional[Callable[[], str]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        recent: Any = None,
        plan: bool = False,
    ) -> None:
        self.generate = generate
        self.recent = recent
        self.plan = plan
        self.runtime = runtime
        self.memory = memory
        self.confirm = confirm
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.history = history
        self.auto_finish = auto_finish
        self.context_fn = context_fn
        self.on_event = on_event
        self._checkpoint_path = checkpoint_path
        self._conn: Optional[sqlite3.Connection] = None
        self.thread_suffix = 0
        self.app = self._compile()

    # -- graph -----------------------------------------------------------------

    def _compile(self) -> Any:
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(AgentState)
        graph.add_node("prepare", self._prepare)
        graph.add_node("think", self._think)
        graph.add_node("act", self._act)
        graph.add_node("finish", self._finish)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "think")
        graph.add_conditional_edges("think", self._after_think, ["act", "finish"])
        graph.add_conditional_edges("act", self._after_act, ["think", "finish"])
        graph.add_edge("finish", END)

        checkpointer = None
        if self._checkpoint_path is not None:
            from langgraph.checkpoint.sqlite import SqliteSaver

            self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._checkpoint_path), check_same_thread=False
            )
            checkpointer = SqliteSaver(self._conn)
        else:
            from langgraph.checkpoint.memory import InMemorySaver

            checkpointer = InMemorySaver()
        return graph.compile(checkpointer=checkpointer)

    def _emit(self, kind: str, **data: Any) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, data)
            except Exception:
                logger.debug("on_event failed", exc_info=True)

    def _prepare(self, state: AgentState) -> Dict[str, Any]:
        request = state.get("request", "")
        notes = []
        if self.context_fn is not None:
            try:
                live = self.context_fn()
                if live:
                    notes.append(live)
            except Exception:
                logger.debug("context_fn failed", exc_info=True)
        try:
            available = list(self.runtime.names)
        except Exception:
            available = None
        hint = tool_hint(request, available)
        if hint:
            notes.append(hint)
        recent = ""
        if self.recent is not None:
            try:
                recent = self.recent.summary()
            except Exception:
                logger.debug("recent summary failed", exc_info=True)
        if recent:
            notes.append(recent)
        if self.memory is not None:
            try:
                remembered = self.memory.context_for(request)
                if remembered:
                    notes.append("You remember:\n" + remembered)
            except Exception:
                logger.debug("memory recall failed", exc_info=True)
        plan = self._make_plan(request, recent, available)
        if plan:
            notes.append(
                "Plan (do every step, in order; reply only when all are done):\n" + plan
            )
        return {
            "messages": [{"role": "user", "content": request}],
            "note": "\n".join(notes),
            "steps": 0,
            "reply": "",
            "done": False,
            "blocked": 0,
            "tool_log": [],
            "checked": False,
        }

    def _make_plan(
        self, request: str, recent: str, available: Optional[List[str]]
    ) -> str:
        """A short numbered plan for multi-part requests (one quick model call).

        Small models do the first part of "open X and search Y, then compare
        it with Z" and stop; a plan in front of them keeps the rest in view.
        """
        if not self.plan or is_single_step(request) or len(request.split()) < 5:
            return ""
        tools = ", ".join(available or []) or "the available tools"
        context = f"\n\nContext:\n{recent}" if recent else ""
        started = time.perf_counter()
        try:
            result = self.generate(
                to_messages(
                    [
                        {"role": "system", "content": PLAN_PROMPT.format(tools=tools)},
                        {"role": "user", "content": f"{request}{context}"},
                    ]
                ),
                tools=None,
            )
        except Exception as exc:
            logger.debug("planning failed: %s", exc)
            return ""
        steps = [
            line.strip()
            for line in clean_reply_lines(result.get("content", "") or "")
            if re.match(r"^\d+[.)]", line.strip())
        ][:6]
        self._emit("plan", seconds=time.perf_counter() - started, steps=steps)
        return "\n".join(steps) if len(steps) >= 2 else ""

    def _model_messages(self, state: AgentState) -> List[Message]:
        history = window(state.get("messages", []), self.history)
        note = state.get("note", "")
        if note:
            # The note rides on the current request only, so earlier turns
            # (and the system prompt + tools) stay a stable cached prefix.
            last_user = max(
                (i for i, m in enumerate(history) if m.get("role") == "user"),
                default=None,
            )
            if last_user is not None:
                message = dict(history[last_user])
                message["content"] = f"{message.get('content', '')}\n\n[{note}]"
                history[last_user] = message
        return to_messages(
            [{"role": "system", "content": self.system_prompt}, *history]
        )

    def _think(self, state: AgentState) -> Dict[str, Any]:
        steps = int(state.get("steps", 0)) + 1
        use_tools = steps <= self.max_steps
        started = time.perf_counter()
        try:
            messages = self._model_messages(state)
            tools = self.runtime.schemas if use_tools else None
            result = self.generate(messages, tools=tools)
            if (
                steps == 1
                and not result.get("tool_calls")
                and not clean_reply(result.get("content", "") or "")
            ):
                # A small model occasionally returns nothing at all; one
                # retry is cheaper than answering "Done." to a real request.
                result = self.generate(messages, tools=tools)
        except Exception as exc:
            logger.warning("Model call failed: %s", exc)
            return {
                "messages": [
                    {"role": "assistant", "content": f"Sorry, the model failed: {exc}"}
                ],
                "steps": steps,
            }
        content = result.get("content", "") or ""
        calls = list(result.get("tool_calls") or []) if use_tools else []
        if use_tools and not calls:
            calls = parse_text_tool_calls(content)
        checked = bool(state.get("checked"))
        if use_tools and not calls and not checked and self._wants_facts(state):
            missing = ungrounded_numbers(clean_reply(content), self._sources(state))
            if missing:
                # "Nvidia trades at $950" with no tool result saying so: send
                # the draft back once and let the model look it up.
                checked = True
                nudge = to_messages(
                    [
                        {"role": "assistant", "content": clean_reply(content)},
                        {
                            "role": "user",
                            "content": (
                                f"[Check: {', '.join(missing[:4])} did not come from "
                                "any tool result. Look them up with web_search, or "
                                "answer without numbers you didn't look up.]"
                            ),
                        },
                    ]
                )
                try:
                    retry = self.generate(messages + nudge, tools=tools)
                except Exception as exc:
                    logger.debug("grounding retry failed: %s", exc)
                else:
                    content = retry.get("content", "") or ""
                    calls = list(retry.get("tool_calls") or [])
                    if not calls:
                        calls = parse_text_tool_calls(content)
        message: Dict[str, Any] = {"role": "assistant", "content": clean_reply(content)}
        if calls:
            message["tool_calls"] = [
                {
                    "id": c.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "name": c.get("name", ""),
                    "arguments": c.get("arguments") or "{}",
                }
                for c in calls
            ]
        self._emit(
            "think",
            seconds=time.perf_counter() - started,
            tool_calls=[c["name"] for c in message.get("tool_calls", [])],
        )
        return {"messages": [message], "steps": steps, "checked": checked}

    def _wants_facts(self, state: AgentState) -> bool:
        """A question about prices/news/scores and a web_search tool to answer it."""
        try:
            names = list(self.runtime.names)
        except Exception:
            return False
        return "web_search" in names and bool(_FACTS.search(state.get("request", "")))

    def _sources(self, state: AgentState) -> List[str]:
        """Everything a number in the reply may legitimately come from."""
        texts = [state.get("request", ""), state.get("note", "")]
        for message in state.get("messages", [])[-self.history :]:
            if message.get("role") in ("tool", "user"):
                texts.append(str(message.get("content") or ""))
        return texts

    def _after_think(self, state: AgentState) -> str:
        last = (state.get("messages") or [{}])[-1]
        return "act" if last.get("tool_calls") else "finish"

    def run_tool(self, name: str, args: Dict[str, Any]) -> tuple[str, bool]:
        """Confirm if needed, then call one tool. Returns (text, ok)."""
        from openjarvis.desktop.server import confirmation_question

        question = confirmation_question(name, args, self.runtime.annotations(name))
        if question:
            approved = False
            if self.confirm is not None:
                try:
                    approved = bool(self.confirm(question))
                except Exception:
                    approved = False
            if not approved:
                self._emit("declined", tool=name)
                text = "The user did not allow this, so it was not done."
                self._note(name, args, text, False)
                return text, False
        self._emit("tool_start", tool=name, args=args)
        outcome = self.runtime.call(name, args)
        self._emit(
            "tool_end",
            tool=name,
            ok=outcome.ok,
            seconds=outcome.seconds,
            text=outcome.text[:200],
        )
        self._note(name, args, outcome.text, outcome.ok)
        return outcome.text, outcome.ok

    def _note(self, name: str, args: Dict[str, Any], text: str, ok: bool) -> None:
        if self.recent is not None:
            try:
                self.recent.note(name, args, text, ok)
            except Exception:
                logger.debug("recent.note failed", exc_info=True)

    def _act(self, state: AgentState) -> Dict[str, Any]:
        last = state["messages"][-1]
        results: List[Dict[str, Any]] = []
        log = list(state.get("tool_log") or [])
        new_entries = []
        blocked = int(state.get("blocked", 0))
        previous = log[-1] if log else None
        for call in last.get("tool_calls", []):
            name = call["name"]
            try:
                args = json.loads(call.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
            except ValueError as exc:
                text, ok, args = f"Invalid arguments: {exc}", False, {}
            else:
                args = with_defaults(name, args, state.get("request", ""))
                try:
                    args = fit_choices(name, args, list(self.runtime.schemas))
                except Exception:
                    logger.debug("fit_choices failed", exc_info=True)
                refusal = unrequested(name, args, state.get("request", ""))
                if refusal is None:
                    refusal = over_limit(name, log + new_entries)
                if refusal is None:
                    refusal = blind_click(
                        name, args, log + new_entries, state.get("request", "")
                    )
                if refusal is None:
                    refusal = file_by_clicking(name, args)
                if refusal is not None:
                    text, ok = refusal, False
                elif previous is not None and _same_call(previous, name, args):
                    # Small models re-run the same look/step when unsure;
                    # nothing changed in between, so the result can't either.
                    blocked += 1
                    text, ok = (
                        "Not repeated: you just made this exact call and nothing "
                        "changed since. Reply to the user, or try a different "
                        "step.",
                        False,
                    )
                else:
                    text, ok = self.run_tool(name, args)
            entry = {"name": name, "args": args, "ok": ok, "text": text}
            previous = entry
            new_entries.append(entry)
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": name,
                    "content": text if ok else f"Error: {text}",
                }
            )
        log.extend(new_entries)
        update: Dict[str, Any] = {
            "messages": results,
            "tool_log": log,
            "blocked": blocked,
        }
        if blocked >= 2:
            update["done"] = True  # stuck: answer with what we have
            return update
        question = next(
            (
                e["text"]
                for e in new_entries
                if not e["ok"] and _ASKS_USER.search(e["text"])
            ),
            None,
        )
        if question:
            # "Did you mean A or B? Say which." is for the user: the model
            # once opened three suggested chats on its own instead of asking.
            update["done"] = True
            update["reply"] = question
            return update

        from openjarvis.desktop.server import is_terminal

        if (
            self.auto_finish
            and int(state.get("steps", 0)) == 1
            and new_entries
            and all(e["ok"] and is_terminal(e["name"], e["args"]) for e in new_entries)
            and is_single_step(state.get("request", ""))
        ):
            update["done"] = True
            update["reply"] = " ".join(e["text"] for e in new_entries)
        return update

    def _after_act(self, state: AgentState) -> str:
        return "finish" if state.get("done") else "think"

    def _finish(self, state: AgentState) -> Dict[str, Any]:
        reply = state.get("reply") or ""
        update: Dict[str, Any] = {}
        log = state.get("tool_log") or []
        last = (state.get("messages") or [{}])[-1]
        if not reply and last.get("role") == "assistant":
            reply = clean_reply(last.get("content", ""))
        checked = honest_reply(reply, log) if reply else reply
        if (
            reply
            and checked == reply
            and last.get("role") == "assistant"
            and not last.get("tool_calls")
        ):
            update["reply"] = reply  # the model's own answer, already stored
        else:
            reply = checked or _fallback_reply(log)
            update["reply"] = reply
            update["messages"] = [{"role": "assistant", "content": reply}]
        if self.memory is not None and log:
            tools = ", ".join(sorted({e["name"] for e in log}))
            try:
                self.memory.remember(
                    f"{state.get('request', '')} -> {reply} (used {tools})",
                    kind="episode",
                )
            except Exception:
                logger.debug("episode log failed", exc_info=True)
        return update

    # -- running ---------------------------------------------------------------

    def thread_id(self) -> str:
        suffix = f"-{self.thread_suffix}" if self.thread_suffix else ""
        return f"desktop-{datetime.now():%Y-%m-%d}{suffix}"

    def _config(self) -> Dict[str, Any]:
        return {
            "configurable": {"thread_id": self.thread_id()},
            "recursion_limit": 4 * self.max_steps + 10,
        }

    def invoke(self, request: str) -> Dict[str, Any]:
        self._close_interrupted_step()
        return self.app.invoke({"request": request}, self._config())

    def _close_interrupted_step(self) -> None:
        """Give an interrupted request's tool calls an explicit result.

        Speaking over Jarvis stops a request mid-step; its tool call stayed in
        the thread without a result and the model "finished" it on the next
        request ("Open Amma" re-ran open_chat('Ammatak')).
        """
        try:
            messages = self.history_messages()
        except Exception:
            return
        if not messages:
            return
        last = messages[-1]
        calls = last.get("tool_calls") if last.get("role") == "assistant" else None
        if not calls:
            return
        closed: List[Dict[str, Any]] = [
            {
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": call.get("name", ""),
                "content": "Error: interrupted - the user spoke over it and moved "
                "on. Don't redo it unless they ask again.",
            }
            for call in calls
        ]
        closed.append({"role": "assistant", "content": "(Stopped.)"})
        try:
            self.app.update_state(
                self._config(), {"messages": closed}, as_node="finish"
            )
        except Exception:
            logger.debug("Could not close the interrupted step", exc_info=True)

    def record(
        self, request: str, name: str, args: Dict[str, Any], text: str, ok: bool
    ) -> None:
        """Add a fast-path exchange to the thread so follow-ups have context."""
        self._close_interrupted_step()
        call_id = f"call_{uuid.uuid4().hex[:8]}"
        messages = [
            {"role": "user", "content": request},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": call_id, "name": name, "arguments": json.dumps(args)}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": text if ok else f"Error: {text}",
            },
            {"role": "assistant", "content": text},
        ]
        try:
            self.app.update_state(
                self._config(), {"messages": messages}, as_node="finish"
            )
        except Exception:
            logger.debug("Could not record fast-path turn", exc_info=True)

    def history_messages(self) -> List[Dict[str, Any]]:
        try:
            snapshot = self.app.get_state(self._config())
        except Exception:
            return []
        return list((snapshot.values or {}).get("messages", []))

    def reset(self) -> None:
        """Start a fresh conversation thread (``/clear``)."""
        self.thread_suffix += 1

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


__all__ = [
    "AgentState",
    "DesktopGraph",
    "PLAN_PROMPT",
    "SYSTEM_PROMPT",
    "clean_reply",
    "honest_reply",
    "unrequested",
    "is_single_step",
    "parse_text_tool_calls",
    "to_messages",
    "window",
]
