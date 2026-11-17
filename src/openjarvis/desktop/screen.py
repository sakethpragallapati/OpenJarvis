"""See and act on the screen: screenshots + vision model, UI Automation.

Two ways to find something to click:

- **UI Automation** (``list_controls`` / ``click_control``): the accessibility
  tree of the active window - exact names and positions of buttons, links,
  text boxes and menu items. Fast and precise; works for most apps and for
  web pages in Chrome/Edge.
- **Vision** (``locate`` / ``describe``): a screenshot sent to the vision
  model (qwen3.5 reads images). Works on anything visible, including games
  and canvases, at ~3-4 s per look.
"""

from __future__ import annotations

import base64
import difflib
import io
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from openjarvis.desktop.winapi import COM, DesktopError, click, ensure_dpi_aware

logger = logging.getLogger(__name__)

# (prompt, base64 image, max_tokens) -> model text
VisionAsk = Callable[[str, str, int], str]


@dataclass(slots=True)
class Shot:
    b64: str
    width: int  # screen pixels
    height: int
    image_width: int
    image_height: int


_HORIZONTAL = re.compile(r"\b(left|right)(?:[- ]hand)?\b", re.I)
_VERTICAL = re.compile(r"\b(top|upper|bottom|lower)\b", re.I)


def region_for(query: str) -> Optional[str]:
    """'the left side of the chat list' -> 'left'; 'bottom right' -> 'bottom right'."""
    parts = []
    vertical = _VERTICAL.search(query or "")
    horizontal = _HORIZONTAL.search(query or "")
    if vertical:
        parts.append(
            "top" if vertical.group(1).lower() in ("top", "upper") else "bottom"
        )
    if horizontal:
        parts.append(horizontal.group(1).lower())
    return " ".join(parts) or None


def capture(
    max_side: int = 1280, quality: int = 80, region: Optional[str] = None
) -> Shot:
    """Screenshot of the primary screen (or its left/right/top/bottom part),
    downscaled and JPEG-encoded. A cropped half keeps more pixels per
    element, which is what reading a side pane needs."""
    ensure_dpi_aware()
    from PIL import ImageGrab

    image = ImageGrab.grab()
    width, height = image.size
    if region:
        left, top, right, bottom = 0, 0, width, height
        if "left" in region:
            right = width // 2
        elif "right" in region:
            left = width // 2
        if "top" in region:
            bottom = height // 2
        elif "bottom" in region:
            top = height // 2
        image = image.crop((left, top, right, bottom))
    image.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=quality)
    return Shot(
        b64=base64.b64encode(buffer.getvalue()).decode(),
        width=width,
        height=height,
        image_width=image.size[0],
        image_height=image.size[1],
    )


def describe(
    ask: VisionAsk, question: str = "", *, context: str = "", factual: bool = False
) -> str:
    """Answer ``question`` about the screen.

    ``factual`` is for the agent checking its own work ("is a Save As dialog
    open?"): a plain observation, no chat, "not visible" when it isn't.
    """
    region = region_for(question)
    shot = capture(region=region)
    where = (
        f"This is the {region} part of my Windows screen. "
        if region
        else ("This is a screenshot of my Windows screen. ")
    )
    if factual:
        prompt = (
            where
            + (f"{context} " if context else "")
            + f"Question: {question.strip() or 'what is on the screen?'} "
            "Answer factually in one or two short sentences, starting with Yes, "
            "No or 'Not visible'. Quote the exact titles, text or numbers you "
            "see that answer it. Do not guess, do not encourage, do not describe "
            "anything else. Ignore any terminal or command-prompt window."
        )
        return ask(prompt, shot.b64, 160).strip()
    prompt = (
        where
        + (f"{context} " if context else "")
        + (
            question.strip()
            or "What is on my screen? Name the main app and what it shows, and "
            "anything that needs my attention."
        )
        + " Ignore any terminal or command-prompt window (that's the assistant "
        "itself). Answer only from what is actually visible; if the question "
        "asks about something that isn't on screen, say it isn't visible. Talk "
        "to me directly (say 'you'), in at most two short sentences."
    )
    return ask(prompt, shot.b64, 220).strip()


def read_text(ask: VisionAsk, what: str = "") -> str:
    shot = capture(max_side=1600, quality=85, region=region_for(what))
    target = what.strip() or "the main window"
    prompt = (
        f"Read out the text in {target} on this screenshot, verbatim, in "
        "reading order. Skip menus, toolbars and the taskbar."
    )
    return ask(prompt, shot.b64, 600).strip()


_NUM = r"-?\d+(?:\.\d+)?"


def parse_point(text: str, shot: Shot) -> Optional[tuple[int, int]]:
    """Screen pixel for a model answer: point_2d / bbox_2d / {x, y} / [x, y].

    Qwen-VL models answer on a 0-1000 grid; values above 1000 are taken as
    pixels of the (downscaled) image instead.
    """
    if re.search(r"\bnull\b|not (?:visible|found)|none", text, re.I) and not re.search(
        _NUM, text
    ):
        return None
    numbers: List[float] = []
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M))
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        if isinstance(data, dict):
            for key in ("point_2d", "point", "bbox_2d", "bbox"):
                if isinstance(data.get(key), list):
                    numbers = [float(v) for v in data[key]]
                    break
            else:
                if "x" in data and "y" in data:
                    numbers = [float(data["x"]), float(data["y"])]
        elif isinstance(data, list):
            numbers = [float(v) for v in data if isinstance(v, (int, float))]
    except (ValueError, TypeError):
        numbers = [float(v) for v in re.findall(_NUM, text)]
    if len(numbers) >= 4:
        x = (numbers[0] + numbers[2]) / 2
        y = (numbers[1] + numbers[3]) / 2
    elif len(numbers) >= 2:
        x, y = numbers[0], numbers[1]
    else:
        return None
    if max(x, y) <= 1000:
        px, py = x / 1000 * shot.width, y / 1000 * shot.height
    else:
        px = x / shot.image_width * shot.width
        py = y / shot.image_height * shot.height
    if not (0 <= px <= shot.width and 0 <= py <= shot.height):
        return None
    return int(px), int(py)


def locate(ask: VisionAsk, target: str) -> Optional[tuple[int, int]]:
    shot = capture()
    prompt = (
        f"Locate {target.strip()} in this screenshot. Answer with only JSON "
        '{"point_2d": [x, y]} using coordinates normalized to 0-1000, or '
        '{"point_2d": null} if it is not visible.'
    )
    answer = ask(prompt, shot.b64, 40)
    logger.debug("Vision locate %r -> %s", target, answer)
    return parse_point(answer, shot)


# ---------------------------------------------------------------------------
# UI Automation
# ---------------------------------------------------------------------------

_INTERACTIVE = {
    "ButtonControl",
    "HyperlinkControl",
    "EditControl",
    "MenuItemControl",
    "TabItemControl",
    "ListItemControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "ComboBoxControl",
    "TreeItemControl",
    "SplitButtonControl",
    "DocumentControl",
    "DataItemControl",
}
_MAX_NODES = 2500


@dataclass(slots=True)
class UIControl:
    index: int
    kind: str
    name: str
    rect: tuple[int, int, int, int]
    automation_id: str = ""

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2

    def describe(self) -> str:
        return f"{self.index}. {self.kind} '{self.name}'"


_LAST: List[UIControl] = []
_LAST_LIVE: List[Any] = []


def _collect(window: Any) -> tuple[List[UIControl], List[Any]]:
    import uiautomation as auto

    controls: List[UIControl] = []
    live: List[Any] = []
    seen = 0
    for control, _depth in auto.WalkControl(window, maxDepth=30):
        seen += 1
        if seen > _MAX_NODES:
            break
        kind = control.ControlTypeName
        if kind not in _INTERACTIVE:
            continue
        name = (control.Name or "").strip()
        if not name and kind != "EditControl":
            continue
        try:
            if control.IsOffscreen:
                continue
            rect = control.BoundingRectangle
            box = (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            continue
        if box[2] - box[0] <= 1 or box[3] - box[1] <= 1:
            continue
        controls.append(
            UIControl(
                index=len(controls) + 1,
                kind=kind.removesuffix("Control"),
                name=name[:80] or "(text box)",
                rect=box,
                automation_id=control.AutomationId or "",
            )
        )
        live.append(control)
    return controls, live


def focused_control() -> tuple[str, str]:
    """(control type, name) of whatever has keyboard focus right now."""

    def work() -> tuple[str, str]:
        import uiautomation as auto

        control = auto.GetFocusedControl()
        if control is None:
            return "", ""
        return control.ControlTypeName, (control.Name or "").strip()

    try:
        return COM.run(work, timeout=10)
    except Exception:
        return "", ""


def _scan_foreground() -> tuple[str, List[UIControl]]:
    """Runs on the COM thread: refresh the numbered control list."""
    import uiautomation as auto

    window = auto.GetForegroundControl()
    top = window.GetTopLevelControl() or window
    controls, live = _collect(top)
    _LAST[:] = controls
    _LAST_LIVE[:] = live
    return top.Name or "the active window", controls


def list_controls(limit: int = 40) -> tuple[str, List[UIControl]]:
    """Clickable/typeable controls of the active window (numbered)."""
    ensure_dpi_aware()
    title, controls = COM.run(_scan_foreground)
    return title, controls[:limit]


def _match(controls: List[UIControl], wanted: str) -> Optional[int]:
    wanted = wanted.strip().lower()
    if not controls or not wanted:
        return None
    if wanted.isdigit():
        index = int(wanted) - 1
        return index if 0 <= index < len(controls) else None
    names = [c.name.lower() for c in controls]
    for i, name in enumerate(names):
        if name == wanted:
            return i
    for i, name in enumerate(names):
        if wanted in name:
            return i
    words = wanted.split()
    for i, name in enumerate(names):
        if all(w in name for w in words):
            return i
    close = difflib.get_close_matches(wanted, names, n=1, cutoff=0.7)
    return names.index(close[0]) if close else None


def _find(name: str, kinds: Optional[set] = None) -> Optional[tuple[UIControl, Any]]:
    if not (name.strip().isdigit() and _LAST):
        _scan_foreground()
    pairs = [
        (c, live)
        for c, live in zip(_LAST, _LAST_LIVE)
        if kinds is None or c.kind in kinds
    ]
    index = _match([c for c, _ in pairs], name)
    return pairs[index] if index is not None else None


def click_control(name: str, *, double: bool = False, right: bool = False) -> str:
    def work() -> Optional[tuple[UIControl, bool]]:
        found = _find(name)
        if found is None:
            return None
        control, live = found
        invoked = False
        if not double and not right:
            try:
                pattern = live.GetInvokePattern()
                if pattern is not None:
                    pattern.Invoke()
                    invoked = True
            except Exception:
                invoked = False
        return control, invoked

    result = COM.run(work)
    if result is None:
        raise DesktopError(f"No control called {name!r} in the active window.")
    control, invoked = result
    if not invoked:
        x, y = control.center
        click(x, y, button="right" if right else "left", double=double)
    return f"Clicked {control.kind.lower()} '{control.name}'."


def type_into(name: str, text: str) -> str:
    def work() -> Optional[tuple[UIControl, bool]]:
        found = _find(name or "1", {"Edit", "ComboBox", "Document"})
        if found is None:
            return None
        control, live = found
        try:
            live.SetFocus()
            pattern = live.GetValuePattern()
            if pattern is not None and not pattern.IsReadOnly:
                pattern.SetValue(text)
                return control, True
        except Exception:
            pass
        return control, False

    result = COM.run(work)
    if result is None:
        raise DesktopError(f"No text box called {name!r} in the active window.")
    control, done = result
    if not done:
        from openjarvis.desktop.winapi import type_text

        x, y = control.center
        click(x, y)
        type_text(text)
    return f"Typed into '{control.name}'."


__all__ = [
    "Shot",
    "UIControl",
    "capture",
    "click_control",
    "describe",
    "list_controls",
    "locate",
    "parse_point",
    "read_text",
    "type_into",
]
