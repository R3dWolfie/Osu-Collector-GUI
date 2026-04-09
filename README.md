# Osu-Collector-GUI

[![Build Windows .exe](https://github.com/R3dWolfie/Osu-Collector-GUI/actions/workflows/build-windows.yml/badge.svg)](https://github.com/R3dWolfie/Osu-Collector-GUI/actions/workflows/build-windows.yml)

A simple cross-platform GUI for downloading [osu!collector](https://osucollector.com/) collections, with progress bars and optional auto-import to **osu!lazer**.

> **Windows users**: pre-built `.exe` is on every [GitHub Actions](https://github.com/R3dWolfie/Osu-Collector-GUI/actions) run as a build artifact, and tagged releases get a published binary on the [Releases](https://github.com/R3dWolfie/Osu-Collector-GUI/releases) page.

Talks to osu!collector's HTTP API directly and downloads `.osz` files from a public osu! mirror — no interactive prompting, no PTY hacks. Runs on **Linux**, **Windows**, and **macOS**.

## Features

- Paste any number of collection URLs/IDs at once and queue the whole batch
- Per-collection and per-beatmap progress bars
- Auto-import each downloaded `.osz` into a running **osu!lazer** instance
- Output folder picker, remembers last used location
- Live log of every action
- Cancel button (stops cleanly between beatmaps)
- Optional consolidation of any `.osdb` files into a single `db/` subfolder

## Install (from source)

```sh
git clone https://github.com/R3dWolfie/Osu-Collector-GUI.git
cd Osu-Collector-GUI
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python osu_collector_gui.py
```

## Build a Windows .exe

See [build_windows.md](build_windows.md) — single PyInstaller command.

## Linux: add to your application menu

Drop the included `osu-collector-gui.desktop` into `~/.local/share/applications/` and update the `Exec=` line to point at your install.

## Auto-import to osu!lazer

If `osu!lazer` is running when downloads start, each `.osz` is sent to it as soon as it lands. The importer auto-detects the lazer binary in standard locations:

- **Linux**: `~/Applications/osu*.AppImage` or the Flatpak shim `sh.ppy.osu`
- **Windows**: `%LOCALAPPDATA%\osulazer\osu!.exe`
- **macOS**: `/Applications/osu!.app`

## Acknowledgements

- [osu!collector](https://osucollector.com/) for the API and the collections themselves
- [catboy.best](https://catboy.best/), [nerinyan.moe](https://api.nerinyan.moe/), [osu.direct](https://osu.direct/) for the beatmap mirrors
- Inspired by [roogue/osu-collector-dl](https://github.com/roogue/osu-collector-dl)

## License

MIT — see [LICENSE](LICENSE).
