"""Resolving "open X" and ranking file-search hits."""

from __future__ import annotations

from pathlib import Path

import pytest

from openjarvis.desktop import places
from openjarvis.desktop.places import Found, clean_target, resolve


@pytest.mark.parametrize(
    ("text", "target", "kind"),
    [
        ("the Minecraft server folder.", "Minecraft server", "folder"),
        ("Open my Downloads folder", "Downloads", "folder"),
        ("please open notepad app for me", "notepad", "app"),
        ("go to the github website", "github", "website"),
        ("Task Manager", "Task Manager", ""),
    ],
)
def test_clean_target(text: str, target: str, kind: str) -> None:
    assert clean_target(text) == (target, kind)


def _no_app(name: str):
    return None


def _no_search(*args, **kwargs):
    return []


def test_known_folders_and_sites_and_urls() -> None:
    assert resolve("downloads", find_app=_no_app).target == "shell:Downloads"
    site = resolve("YouTube", find_app=_no_app, search_fn=_no_search)
    assert site.kind == "website" and "youtube.com" in site.target
    url = resolve("open github.com/openjarvis", find_app=_no_app)
    assert url.target == "https://github.com/openjarvis"
    assert resolve("the browser").target.startswith("https://")


def test_installed_app_beats_website_with_same_name() -> None:
    found = resolve(
        "Netflix", find_app=lambda n: ("Netflix", r"shell:AppsFolder\Netflix!App")
    )
    assert found.kind == "app" and found.target.startswith("shell:AppsFolder")


def test_misheard_app_name_is_corrected() -> None:
    seen = []

    def finder(name: str):
        seen.append(name)
        return ("Visual Studio Code", r"C:\Start\Visual Studio Code.lnk")

    found = resolve("Open VS Core.", find_app=finder, search_fn=_no_search)
    assert seen == ["vs code"] and found.display == "Visual Studio Code"


def test_file_manager_means_file_explorer() -> None:
    seen = []
    resolve("file manager", find_app=lambda n: seen.append(n) or ("x", "explorer.exe"))
    assert seen == ["file explorer"]


def test_folder_search_is_used_and_marked_learned(tmp_path: Path) -> None:
    folder = tmp_path / "Minecraft Server"
    hit = Found(str(folder), True, 1.8)
    found = resolve(
        "the Minecraft server folder",
        find_app=_no_app,
        search_fn=lambda q, kind, limit: [hit] if kind == "folder" else [],
    )
    assert found.target == str(folder) and found.learned and found.confident


def test_remembered_place_wins_over_search(tmp_path: Path) -> None:
    found = resolve(
        "minecraft server folder",
        recall_place=lambda name: str(tmp_path) if name == "minecraft server" else None,
        find_app=_no_app,
        search_fn=lambda *a: pytest.fail("should not search"),
    )
    assert found.kind == "place" and found.target == str(tmp_path)


def test_existing_path_opens_directly(tmp_path: Path) -> None:
    file = tmp_path / "notes.txt"
    file.write_text("x")
    found = resolve(str(file), find_app=_no_app)
    assert found.kind == "file" and found.target == str(file)


def test_scoring_prefers_whole_word_matches(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(places, "_home", lambda: tmp_path)
    exact = places._score("minecraft server", str(tmp_path / "Minecraft Server"), True)
    partial = places._score("vs core", str(tmp_path / "bin" / "vscore.dll"), False)
    hidden = places._score("server", str(tmp_path / "mc" / ".fabric" / "server"), True)
    assert exact >= 1.0 > partial
    assert hidden < exact
