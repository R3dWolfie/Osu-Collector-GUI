"""Verify OsuLazerImporter._locate_binary finds the osu!lazer executable
across installer layouts — especially the Velopack `current\\` folder used
by all current Windows installs (regression for maps not importing)."""
from pathlib import Path
from unittest.mock import patch

from osu_collector_gui import OsuLazerImporter


def _locate_with(home: Path, platform: str) -> Path | None:
    with patch("osu_collector_gui.sys.platform", platform), \
         patch("osu_collector_gui.Path.home", return_value=home):
        return OsuLazerImporter._locate_binary()


def test_win_velopack_current_layout(tmp_path):
    """Modern osu!lazer (Velopack) keeps the exe in %LOCALAPPDATA%\\osulazer\\current."""
    exe = tmp_path / "AppData/Local/osulazer/current/osu!.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    assert _locate_with(tmp_path, "win32") == exe


def test_win_legacy_squirrel_layout(tmp_path):
    """Legacy Squirrel installs used an app-X.Y.Z subfolder."""
    exe = tmp_path / "AppData/Local/osulazer/app-2024.1.1/osu!.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    assert _locate_with(tmp_path, "win32") == exe


def test_win_current_preferred_over_legacy(tmp_path):
    """When both layouts exist, the live Velopack `current` exe wins."""
    base = tmp_path / "AppData/Local/osulazer"
    current = base / "current/osu!.exe"
    legacy = base / "app-2023.1.1/osu!.exe"
    for p in (current, legacy):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"MZ")
    assert _locate_with(tmp_path, "win32") == current


def test_win_none_when_not_installed(tmp_path):
    assert _locate_with(tmp_path, "win32") is None
