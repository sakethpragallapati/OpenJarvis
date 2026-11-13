"""App lookup, URL validation and volume key maths (no real OS calls)."""

from __future__ import annotations

import pytest

from openjarvis.pc import control


@pytest.fixture()
def start_menu(tmp_path, monkeypatch):
    programs = tmp_path / "Programs"
    (programs / "Visual Studio Code").mkdir(parents=True)
    for rel in (
        "Visual Studio Code/Visual Studio Code.lnk",
        "Visual Studio Code/Uninstall Visual Studio Code.lnk",
        "Google Chrome.lnk",
        "Spotify.lnk",
        "Blender 4.2.lnk",
    ):
        (programs / rel).write_text("")
    monkeypatch.setattr(control, "_start_menu_dirs", lambda: [programs])
    monkeypatch.setattr(
        control, "_start_apps", lambda: {"photos": "Microsoft.Photos!App"}
    )
    return programs


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("VS Code", "Visual Studio Code"),
        ("open spotify app", "Spotify"),
        ("chrome", "Google Chrome"),
        ("blender", "Blender 4.2"),
        ("spotfy", "Spotify"),
    ],
)
def test_find_app_matches_spoken_names(start_menu, spoken, expected) -> None:
    found = control.find_app(spoken)
    assert found is not None and found[0] == expected


def test_uninstallers_are_never_matched(start_menu) -> None:
    found = control.find_app("uninstall visual studio code")
    assert found is None or "Uninstall" not in found[0]


def test_builtins_and_store_apps(start_menu, monkeypatch) -> None:
    monkeypatch.setattr(control.sys, "platform", "win32")
    assert control.find_app("calculator") == ("calculator", "calc.exe")
    assert control.find_app("photos") == (
        "Photos",
        "shell:AppsFolder\\Microsoft.Photos!App",
    )
    assert control.find_app("nonexistent thing") is None


@pytest.mark.parametrize(
    "bad", ["file:///C:/Windows", "javascript:alert(1)", "ms-settings:privacy", ""]
)
def test_only_web_urls_open(bad) -> None:
    with pytest.raises(control.PCControlError):
        control.open_website(bad)


def test_bare_domain_gets_https(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(
        control.webbrowser, "open", lambda url: opened.append(url) or True
    )
    assert control.open_website("example.com") == "https://example.com"
    assert control.open_website("localhost:3000") == "https://localhost:3000"
    assert opened == ["https://example.com", "https://localhost:3000"]


def test_set_volume_bottoms_out_then_steps_up(monkeypatch) -> None:
    presses = []
    monkeypatch.setattr(
        control, "_press", lambda key, times=1: presses.append((key, times))
    )
    assert control.set_volume(30) == "Volume set to 30%."
    assert presses == [("volume_down", 50), ("volume_up", 15)]


def test_set_volume_clamps(monkeypatch) -> None:
    presses = []
    monkeypatch.setattr(
        control, "_press", lambda key, times=1: presses.append((key, times))
    )
    control.set_volume(-5)
    assert presses == [("volume_down", 50)]


def test_non_windows_actions_explain_themselves(monkeypatch) -> None:
    monkeypatch.setattr(control.sys, "platform", "linux")
    with pytest.raises(control.PCControlError, match="Windows"):
        control.lock_screen()
