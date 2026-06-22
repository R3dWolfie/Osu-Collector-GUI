# Building a Windows .exe

PyInstaller bundles `osu_collector_gui.py`, the `web/` frontend, and the
Python dependencies into a single self-contained executable. Build it on a
Windows machine (or VM) so the bundled runtime matches the target platform.

The GUI renders through **Edge WebView2**, which ships with Windows 10/11 —
no Qt or extra runtime to bundle, so the binary is far smaller than the old
PyQt build.

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
3. Build (note the `--add-data` flag — the `web/` folder must be bundled):
   ```bat
   pyinstaller --noconfirm --windowed --onefile ^
       --name osu-collector-gui ^
       --add-data "web;web" ^
       osu_collector_gui.py
   ```
4. The executable lands at `dist\osu-collector-gui.exe`. Double-click to run.

## Notes

- `--add-data "web;web"` copies the HTML/CSS/JS frontend into the bundle.
  On Windows the separator is `;` (on Linux/macOS it would be `:`). The app
  finds these files via `sys._MEIPASS` when frozen.
- `--windowed` hides the console window. Drop it if you want a console for
  debugging.
- The first launch unpacks the bundle to `%TEMP%`, so it can take 1–2 s.
- Fonts (Big Shoulders Display, Sora, JetBrains Mono) load from Google
  Fonts at runtime; the UI falls back to system fonts when offline.
- Auto-import expects `osu!lazer` in the standard
  `%LOCALAPPDATA%\osulazer\current\osu!.exe` location. If yours is
  elsewhere, set it in **Settings → Paths**.

## Code-signing (optional)

By default the .exe is unsigned and SmartScreen will warn users on first
run. For personal use that's fine; to distribute more widely, sign it with
a code-signing certificate using `signtool.exe`.
