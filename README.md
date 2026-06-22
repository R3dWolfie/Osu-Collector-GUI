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
- **Native installer** for **Windows** (`Setup.exe`, bundles the Collection Manager CLI + auto-installs WebView2/.NET if missing) and a **macOS** `.dmg`, with a **built-in update checker** that one-click installs new releases. On **Linux**, [run from source](#linux-run-from-source-recommended) — the reliable path across distros

## Install (from source)

```sh
git clone https://github.com/R3dWolfie/Osu-Collector-GUI.git
cd Osu-Collector-GUI
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python osu_collector_gui.py
```

### Linux: run from source (recommended)

`pywebview` renders into the host's **WebKitGTK**, which is tightly tied to the
system GObject stack — so on Linux the reliable path is running from source
against your distro's own libraries. (A bundled AppImage fights newer distros'
WebKit/GLib; this is a known pywebview-on-Linux limitation.)

```sh
# 1. System packages — WebKitGTK + GTK3 + PyGObject:
#   Arch:          sudo pacman -S webkit2gtk-4.1 gtk3 python-gobject
#   Debian/Ubuntu: sudo apt install gir1.2-webkit2-4.1 gir1.2-gtk-3.0 python3-gi python3-gi-cairo
#   Fedora:        sudo dnf install webkit2gtk4.1 gtk3 python3-gobject

# 2. Venv that can see the system GObject bindings, then run:
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
python osu_collector_gui.py
```

Windows uses the built-in **Edge WebView2** runtime and macOS uses system **WebKit**, so the prebuilt installers there need no extra backend setup.

#### Reading / merging osu!lazer collections on Linux (optional)

Downloads and auto-import work with just the steps above. **Listing your existing
lazer collections and merging into them** is done by the Collection Manager CLI —
a Windows **.NET 9** tool — which on Linux runs through the WineHQ flatpak. Run the
bundled setup once; it installs the flatpak + the .NET 9 runtime and grants it
access to your osu! data:

```sh
scripts/setup-linux.sh            # or: scripts/setup-linux.sh /path/to/osu/data
```

Then launch the app and your collections appear in the import dropdown. (On
Windows the installer handles all of this automatically.)

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
