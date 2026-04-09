# Building a Windows .exe

PyInstaller bundles `osu_collector_gui.py` and its dependencies into a
single self-contained executable. Build it on a Windows machine (or in a
Windows VM — cross-compiling from Linux is unreliable for PyQt6 because
the Qt binaries differ per platform).

## Steps

1. Install **Python 3.11+** for Windows from <https://python.org>. Tick
   "Add python.exe to PATH" in the installer.
2. Open `cmd.exe` (or PowerShell) in the cloned repo:
   ```bat
   git clone https://github.com/R3dWolfie/Osu-Collector-GUI.git
   cd Osu-Collector-GUI
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   pip install pyinstaller
   ```
3. Build:
   ```bat
   pyinstaller --noconfirm --windowed --onefile ^
       --name osu-collector-gui ^
       osu_collector_gui.py
   ```
4. The executable lands at `dist\osu-collector-gui.exe`. Double-click to run.

## Notes

- `--windowed` hides the console window when launched. If you want a
  console for debugging, drop that flag.
- `--onefile` produces a single `.exe`. Without it you get a folder
  containing `osu-collector-gui.exe` and its dependencies; the folder
  variant starts faster.
- The first launch unpacks the bundle to `%TEMP%`, so it can take 1–2 s.
- File size is ~50 MB because it bundles all of Qt6.
- Auto-import expects `osu!lazer` installed in the standard
  `%LOCALAPPDATA%\osulazer\osu!.exe` location. If yours is elsewhere,
  edit `OsuLazerImporter._locate_binary` in the source.

## Code-signing (optional)

By default the .exe is unsigned and SmartScreen will warn users on first
run. For personal use that's fine; if you want to distribute it more
widely, sign it with a code-signing certificate using `signtool.exe`.
