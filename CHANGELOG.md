# Changelog

All notable changes to this project will be documented in this file.

## [0.6.2] — 2026-05-12

### Fixed

- **Downloads now spread load across mirrors intelligently.** Previously every parallel download slot started by trying catboy.best, so if catboy was rate-limiting the user's IP all 10 slots would each pay a full TCP-connect timeout (~10s × 3 retries) before falling back. Now `BeatmapMirror` picks the least-busy alive mirror for each new download — when catboy is healthy and fast its active count drops to 0 immediately so it stays preferred; when catboy stalls or its connections pile up, load shifts to nerinyan / osu.direct / beatconnect automatically. No magic numbers, no UI controls, no user-visible behavior changes when mirrors are working normally.

## [0.6.1] — 2026-05-12

### Fixed

- **"Have to resize the window to see content" on first open.** The QScrollArea wrapping the form would underestimate its content height during Qt's initial layout pass, leaving some sections invisible until the user manually resized the window. Now `MainWindow.showEvent` schedules a deferred `updateGeometry()` + `adjustSize()` on the scroll area's inner widget via `QTimer.singleShot(0, …)`, which runs after the layout has settled. Especially load-bearing on Windows where the configure-notify timing differs from Linux.
- **Windows DPI scaling at 125% / 150%.** Qt 6's default `Round` rounding policy was snapping non-integer scaling factors to the nearest integer, producing 1-pixel-off widget heights that compounded across nested forms. Now uses `PassThrough` so Qt honors the OS's exact DPI factor. Set before `QApplication` is constructed, as required.
- **Root scroll-content vertical size policy** changed from `Preferred` to `Minimum` so its `sizeHint()` reflects actual minimum content height (matters for some compositor configurations).

## [0.6.0] — 2026-05-12

The "stop redownloading maps I already have" release. Huge osu!collector collections (e.g. 17391 with ~11k maps) now skip downloading any beatmapset where lazer already has at least one diff imported — but still compose the full collection in lazer.

### Added

- **Skip beatmapsets already imported in osu!lazer.** New checkbox under "Lazer collections". Before downloads start, Collection Manager CLI probes lazer's BeatmapInfo DB (`cm.exe create -b <bids> -l <realm-parent>`) to learn which beatmap_ids it has. Sets with at least one imported diff are skipped; their md5s still land in the resulting lazer collection so it composes correctly. Bonus: the .osdb is written using lazer's current md5 for resolved maps so collection entries aren't "ghost" rows when the mapper has updated diffs.
- **Configurable parallel download count.** New "Parallel downloads" spinbox (1..32, default 4 to preserve previous behavior). Above ~8, requests round-robin across the three configured mirrors (catboy.best / nerinyan.moe / osu.direct) so a single mirror doesn't take all the load.
- **Mirror round-robin** in `BeatmapMirror.download()` — each `set_id` picks a different primary URL via `set_id % len(urls)`, with the other mirrors retained as fallbacks. Disabled by passing `round_robin=False` to the constructor.

### Other

- New `tests/` directory with pytest unit tests for the writer/reader prefer-md5 path, mirror URL rotation, and CM CLI probe (mocked subprocess). `requirements-dev.txt` pins pytest.

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
