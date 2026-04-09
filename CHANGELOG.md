# Changelog

All notable changes to this project will be documented in this file.

## [0.5.0] — 2026-04-09

The "actually merges into osu!lazer" release. Going from "downloads beatmaps" to "downloads beatmaps, generates `.osdb`, **and** writes them into your live `client.realm` non-destructively, then relaunches lazer for you" took most of a day of debugging .NET unhandled exceptions under wine, and it finally works end-to-end.

### Added

- **Merge downloaded collections directly into osu!lazer.** New "Add downloaded maps to osu!lazer collections" section: pick an existing collection from a refreshable dropdown, create a new one inline, or let each osu!collector collection become its own lazer collection. Uses Collection Manager CLI under the hood as a Realm codec, with all the merging done in Python so existing collections are preserved.
- **`.osdb` file generation** in the o!dm8 (gzip) format that Collection Manager itself emits. Works as a generic export even if you don't use the merge feature.
- **Auto-detect of `client.realm`** based on platform — `%APPDATA%\osu` on Windows, `~/.local/share/osu` on Linux, `~/Library/Application Support/osu` on macOS.
- **Auto-download Collection Manager CLI** on first use if it's not already installed (~3.6 MB from CM's GitHub releases). Cached to `~/.cache/osu-collector-gui/cm-cli/` (Linux) or `%LOCALAPPDATA%\osu-collector-gui\cm-cli\` (Windows). On Linux + wine flatpak, the necessary `flatpak override` runs automatically.
- **"Recover realm from backup…" button** that lists every `client.realm.bak-<timestamp>` snapshot with date and size. Restoring also takes a fresh `before-recover-<timestamp>` copy of the current state, so the action is two-way reversible.
- **Per-collection target picker** with collision policy (merge, skip, rename) — "Refresh" populates it from your live realm via CM CLI.
- **"Continue merge?" confirmation prompt** that pauses the worker after auto-imports finish, so you can verify in lazer that all the .osz files are imported before the destructive realm rewrite kicks in.
- **Auto-cleanup** of `<id> - <name>/` download folders after import is confirmed done. The `db/` subfolder, anything containing `.realm`, hidden temp dirs, and non-matching folders are all left untouched.
- **Persistent debug log** at `/tmp/oc-cm-cli-debug.log` (Linux/macOS) or `%TEMP%\oc-cm-cli-debug.log` (Windows) capturing every CM CLI invocation's full stdout + stderr.
- **Settings persisted on window close**, not just on Start click — adjustments stop disappearing if you close without running a download.
- **Title bar** now reads `osu-collector-gui v0.5.0 by Red`.

### Fixed

- **AppImage relaunch failure**. The kill step recorded `psutil.exe` which on a running AppImage is `/tmp/.mount_<hash>/usr/bin/osu!` — a path that gets unmounted the moment the AppImage exits. Relaunch then failed with `ENOENT`. Now reads `$APPIMAGE` env var → `cmdline[0]` → exe symlink (skipping `/tmp/.mount_*` paths) so the relaunch always uses the persistent `.AppImage` file.
- **Stale `.osdb` files getting merged into the realm**. Previously the merge step did `output_dir.rglob("*.osdb")` and silently scooped up `.osdb` files left over from previous batches — a user downloading 1 collection ended up with 13 unrelated collections in lazer. Now tracks every `.osdb` written this run and merges only those.
- **`OsdbWriter` produced files CM CLI couldn't read.** Previous v6 (uncompressed) format triggered `System.IO.EndOfStreamException` deep inside CM's reader because the v6 path is not actively maintained — CM only writes v8 internally. Now writes o!dm8 (gzip-compressed, with `OnlineId` field) which CM reads through its well-tested code path.
- **Destructive merge wiping the realm on a failed export.** A previous version had `except: existing = []` which let an empty/unreadable export silently turn into "you have no existing collections", and the subsequent CM write deleted everything. The merge is now fail-closed: any export problem aborts the run with a clear error and the realm is untouched. A multi-MB realm that exports zero collections is also rejected as a sanity check.
- **`client.realm` never properly released after kill.** `psutil.terminate()` returns instantly but lazer takes a moment to flush its Realm state. Now `wait_procs(timeout=15)` blocks until the processes actually exit, escalating to SIGKILL after 15s, with an additional 2s grace period for kernel handle release. Stale `client.realm.lock` and `client.realm.note` files are also cleaned up.
- **Refresh button hung waiting for `osu!lazer` to be closed.** Now snapshots `client.realm` to a sibling `.snapshot.realm` and reads the COPY — lazer can stay open while you browse collections. Realm is MVCC so the file copy is a consistent point-in-time snapshot.
- **Wine sandbox + temp paths.** CM CLI invoked through the wine flatpak couldn't write to `/tmp` (sandbox can only see directories explicitly granted to it). All temp output is now placed next to `client.realm`, which is always wine-accessible because lazer runs from there.
- **CM CLI command field broke on paths with spaces.** `raw.split()` mangled `Collection Manager` and `shlex.split` ate backslashes from unquoted Windows-style paths. Settings now persist `cm_cli_command` as a JSON list — no string-parsing round trip — and the QLineEdit only quotes for display.
- **CM CLI Realm.NET double-open crash (`0xe0434352`)**. Passing both `-i` and `-l` made CM open `client.realm` twice. Now always passes `-s` (SkipOsuLocation) — the input file is enough.
- **`shlex.join` for the displayed command** so paths with spaces survive the round trip into and out of settings.
- **`xdg-document-portal` FUSE wedge bricking flatpak launches.** Documented; partial workaround via the existing `fix-portals` helper script.
- **Realm path field defaulted to Linux path on Windows.** Now picks the right location per platform.

### Other

- Added `CHANGELOG.md` (this file).
- Title bar version, app version, settings file version, and release tag are all in sync.

## [0.2.0] — 2026-04-09

- Initial public release.
- Single-file Python rewrite using PyQt6 + requests, replacing the original bash + kdialog + Python PTY-driver stack that drove `osu-collector-dl` interactively.
- Talks to osu!collector's HTTP API directly and downloads `.osz` files from `catboy.best` with `nerinyan.moe` and `osu.direct` as fallbacks.
- Per-collection and per-beatmap progress bars in a native Qt window.
- Auto-import each downloaded `.osz` into a running `osu!lazer` instance (Linux AppImage / Flatpak / Windows Squirrel install / macOS app bundle).
- Settings persistence at `~/.config/osu-collector-gui/settings.json`.
- GitHub Actions workflow that builds the Windows `.exe` with PyInstaller and attaches it to releases tagged `v*`.

## [0.1.0] — 2026-04-09

- First commit. Smoke test of the architecture: PyQt6 + requests + threading + a single download path. Dropped almost immediately in favour of 0.2.0.
