# Osu-Collector-GUI

[![Build Windows .exe](https://github.com/R3dWolfie/Osu-Collector-GUI/actions/workflows/build-windows.yml/badge.svg)](https://github.com/R3dWolfie/Osu-Collector-GUI/actions/workflows/build-windows.yml)

A cross-platform GUI for downloading [osu!collector](https://osucollector.com/) collections, with live progress and optional auto-import to **osu!lazer**. Built in the **R3D "Cherry"** design system — a warm near-black look with industrial-condensed display type and monospace technical labelling.

> **Windows users**: pre-built `.exe` is on every [GitHub Actions](https://github.com/R3dWolfie/Osu-Collector-GUI/actions) run as a build artifact, and tagged releases get a published binary on the [Releases](https://github.com/R3dWolfie/Osu-Collector-GUI/releases) page.

The interface is an HTML/CSS/JS frontend rendered in a native [pywebview](https://pywebview.flowlib.org/) window, sitting on top of a pure-Python download engine. It talks to osu!collector's HTTP API directly and downloads `.osz` files from public osu! mirrors. Runs on **Linux**, **Windows**, and **macOS**.

## The flow (it's meant to be effortless)

1. **Paste** an osu!collector link or ID (as many as you like).
2. **Pick** the lazer collection to import into — existing ones are auto-scanned, or create a new one.
3. **Hit Download.**

Everything else — your osu!lazer binary, `client.realm`, the Collection Manager CLI, the output folder, sensible parallelism — auto-detects. Power-user knobs all live behind the **Settings** tab.

## Features

- Paste any number of collection links/IDs at once; live **poster previews** of each collection
- Auto-scans your osu! folder for existing lazer collections to merge into
- Persistent **download dock** with live progress; per-collection / per-beatmap counts
- Auto-import each downloaded `.osz` into a running **osu!lazer** instance
- Dark theme by default with a working **light toggle** (persisted)
- Live activity log, toasts, and a satisfying finish
- Cancel button (stops cleanly between beatmaps)
- Optional `.osdb` generation and consolidation into a single `db/` subfolder

## Install (from source)

```sh
git clone https://github.com/R3dWolfie/Osu-Collector-GUI.git
cd Osu-Collector-GUI
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python osu_collector_gui.py
```

### Linux: pick a webview backend

`pywebview` needs a native webview to render into. Install **one** backend:

```sh
# GTK (recommended) — also needs system packages:
#   Debian/Ubuntu: sudo apt install python3-gi gir1.2-webkit2-4.1
pip install "pywebview[gtk]"

# …or Qt:
pip install "pywebview[qt]"
```

Windows uses the built-in **Edge WebView2** runtime and macOS uses system **WebKit**, so no extra backend is needed there.

## Build standalone binaries (Windows · macOS · Linux)

See [BUILD.md](BUILD.md). The included GitHub Actions workflow builds all
three at once on every push/PR; manual per-OS PyInstaller commands are
documented there too.

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
