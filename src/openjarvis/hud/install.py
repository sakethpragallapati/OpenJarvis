"""The JARVIS icon, and Desktop / Start-menu / startup shortcuts."""

from __future__ import annotations

import math
import os
import random
import sys
from pathlib import Path
from typing import List, Optional

SHORTCUT_NAME = "Jarvis.lnk"
_ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def icon_path() -> Path:
    from openjarvis.hud.app import app_dir

    return app_dir() / "jarvis.ico"


def draw_icon(size: int = 256):  # -> PIL.Image.Image
    """A glowing teal ring of thin wavy strands on black (like the window)."""
    from PIL import Image, ImageDraw, ImageFilter

    scale = 4  # draw big, shrink for smooth lines
    big = size * scale
    rng = random.Random(7)
    strands = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    pen = ImageDraw.Draw(strands)
    centre, radius = big / 2, big * 0.34
    for i in range(28):
        p1, p2, p3 = (rng.random() * 6.28 for _ in range(3))
        base = radius * (1 + (i / 28 - 0.5) * 0.09)
        points = []
        for j in range(181):
            a = j / 180 * 2 * math.pi
            n = (
                math.sin(3 * a + p1) * 0.36
                + math.sin(5 * a - p2) * 0.24
                + math.sin(7 * a + p3) * 0.14
            )
            r = base + radius * 0.07 * n
            points.append((centre + r * math.cos(a), centre + r * math.sin(a)))
        pen.line(points, fill=(0, 229, 204, 120), width=max(2, big // 220))
    inner = big * 0.15
    pen.ellipse(
        (centre - inner, centre - inner, centre + inner, centre + inner),
        outline=(0, 229, 204, 200),
        width=max(3, big // 120),
    )
    glow = strands.filter(ImageFilter.GaussianBlur(big / 40))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=big // 5, fill=255
    )
    tile = Image.new("RGBA", (big, big), (1, 8, 10, 255))
    tile = Image.alpha_composite(tile, glow)
    tile = Image.alpha_composite(tile, glow)
    tile = Image.alpha_composite(tile, strands)
    tile.putalpha(mask)  # the glow stays inside the rounded tile
    return tile.resize((size, size), Image.LANCZOS)


def ensure_icon(force: bool = False) -> Path:
    path = icon_path()
    if force or not path.exists():
        draw_icon(256).save(path, format="ICO", sizes=_ICON_SIZES)
    return path


def launcher() -> List[str]:
    """What a shortcut runs: ``jarvis-app.exe`` (no console), else
    ``pythonw -m openjarvis.hud``."""
    scripts = Path(sys.executable).parent
    exe = scripts / "jarvis-app.exe"
    if exe.exists():
        return [str(exe)]
    pythonw = scripts / "pythonw.exe"
    return [
        str(pythonw if pythonw.exists() else sys.executable),
        "-m",
        "openjarvis.hud",
    ]


def _shell() -> object:
    import comtypes.client

    return comtypes.client.CreateObject("WScript.Shell", dynamic=True)


def _folders() -> dict:
    shell = _shell()
    return {
        "desktop": Path(str(shell.SpecialFolders("Desktop"))),
        "start_menu": Path(str(shell.SpecialFolders("Programs"))),
        "startup": Path(str(shell.SpecialFolders("Startup"))),
    }


def _write_shortcut(
    path: Path, command: List[str], icon: Path, minimized: bool = False
) -> None:
    link = _shell().CreateShortcut(str(path))
    link.TargetPath = command[0]
    link.Arguments = " ".join(command[1:])
    link.WorkingDirectory = str(Path.home())
    link.IconLocation = f"{icon},0"
    link.Description = "J.A.R.V.I.S. - your voice assistant"
    if minimized:
        link.WindowStyle = 7
    link.Save()


def install(*, desktop: bool = True, start_menu: bool = True) -> List[Path]:
    """Create the icon and the shortcuts; returns the shortcut paths."""
    if sys.platform != "win32":
        raise RuntimeError("Shortcuts are only made on Windows.")
    icon = ensure_icon()
    command = launcher()
    folders = _folders()
    made = []
    for key, wanted in (("desktop", desktop), ("start_menu", start_menu)):
        if wanted:
            path = folders[key] / SHORTCUT_NAME
            _write_shortcut(path, command, icon)
            made.append(path)
    return made


def uninstall() -> List[Path]:
    removed = []
    if sys.platform != "win32":
        return removed
    for folder in _folders().values():
        path = folder / SHORTCUT_NAME
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def set_autostart(enabled: bool) -> Optional[Path]:
    """Open Jarvis when you sign in to Windows (a Startup-folder shortcut)."""
    if sys.platform != "win32":
        raise RuntimeError("Autostart is only set up on Windows.")
    path = _folders()["startup"] / SHORTCUT_NAME
    if enabled:
        # Loads everything in the tray, so opening Jarvis later is instant.
        _write_shortcut(path, [*launcher(), "--background"], ensure_icon())
        return path
    if path.exists():
        os.remove(path)
    return None


__all__ = [
    "draw_icon",
    "ensure_icon",
    "icon_path",
    "install",
    "launcher",
    "set_autostart",
    "uninstall",
]
