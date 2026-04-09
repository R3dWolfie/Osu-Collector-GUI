#!/usr/bin/env python3
"""
osu-collector-gui — cross-platform GUI for downloading osu!collector
collections, with progress bars and optional auto-import to osu!lazer.

Talks directly to https://osucollector.com/api/ and downloads .osz files
from a public osu! mirror (catboy.best by default). No interactive
prompting, no PTY driving — just HTTP.

Runs on Linux, Windows, and macOS. Single file. Bundle for Windows with:
    pyinstaller --noconfirm --windowed --onefile osu_collector_gui.py
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "osu-collector-gui"
APP_VERSION = "0.1.0"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+https://github.com/R3dWolfie/Osu-Collector-GUI)"

OSU_COLLECTOR_API = "https://osucollector.com/api"
DEFAULT_MIRROR = "https://catboy.best/d"   # /<beatmapset_id>
FALLBACK_MIRRORS = [
    "https://api.nerinyan.moe/d",
    "https://api.osu.direct/d",
]

# Network limits — be polite to the mirrors
DOWNLOAD_PARALLEL = 4   # how many .osz fetches in parallel within a collection
DOWNLOAD_TIMEOUT_S = 120
HTTP_RETRIES = 3
HTTP_BACKOFF_S = 2

CONFIG_DIR = Path.home() / (
    ".config" if sys.platform != "win32" else "AppData/Roaming"
) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "settings.json"


# ---------------------------------------------------------------------------
# osu!collector API client
# ---------------------------------------------------------------------------

@dataclass
class BeatmapInfo:
    beatmap_id: int
    set_id: int
    md5: str
    artist: str = "Unknown"
    title: str = "Unknown"
    diff_name: str = "Unknown"
    mode: int = 0          # 0=osu, 1=taiko, 2=fruits, 3=mania
    star_rating: float = 0.0


@dataclass
class CollectionInfo:
    id: int
    name: str
    uploader: str
    beatmap_count: int
    beatmapset_ids: list[int] = field(default_factory=list)
    beatmaps: list[BeatmapInfo] = field(default_factory=list)


_MODE_TO_INT = {"osu": 0, "taiko": 1, "fruits": 2, "catch": 2, "mania": 3}


class OsuCollectorClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def fetch_collection(self, collection_id: int,
                         with_beatmap_details: bool = False) -> CollectionInfo:
        """Fetch collection metadata + flat list of beatmapset IDs.

        If with_beatmap_details is True, also fetches per-beatmap details
        (artist, title, diff name, mode, star rating, md5) needed for
        .osdb generation. Costs extra paginated API calls.
        """
        url = f"{OSU_COLLECTOR_API}/collections/{collection_id}"
        r = self.session.get(url, timeout=30)
        if r.status_code == 404:
            raise ValueError(f"Collection {collection_id} not found")
        r.raise_for_status()
        data = r.json()

        # Extract unique beatmapset IDs (collection metadata gives them
        # under "beatmapsets" — each item has its own .id).
        set_ids: list[int] = []
        seen: set[int] = set()
        for bs in data.get("beatmapsets", []):
            sid = bs.get("id")
            if isinstance(sid, int) and sid not in seen:
                seen.add(sid)
                set_ids.append(sid)

        info = CollectionInfo(
            id=int(data["id"]),
            name=str(data.get("name") or f"Collection {collection_id}"),
            uploader=str((data.get("uploader") or {}).get("username") or "?"),
            beatmap_count=int(data.get("beatmapCount") or len(set_ids)),
            beatmapset_ids=set_ids,
        )

        if with_beatmap_details:
            info.beatmaps = self._fetch_beatmaps_paged(collection_id)
        return info

    def _fetch_beatmaps_paged(self, collection_id: int) -> list[BeatmapInfo]:
        """Page through /beatmapsv2 to get details for every beatmap."""
        out: list[BeatmapInfo] = []
        cursor = "0"
        for _ in range(500):  # safety bound — most collections fit in <50 pages
            url = (f"{OSU_COLLECTOR_API}/collections/{collection_id}/beatmapsv2"
                   f"?perPage=100&cursor={cursor}")
            r = self.session.get(url, timeout=30)
            r.raise_for_status()
            data = r.json()
            for b in data.get("beatmaps", []) or []:
                bs = b.get("beatmapset") or {}
                out.append(BeatmapInfo(
                    beatmap_id=int(b.get("id") or 0),
                    set_id=int(b.get("beatmapset_id") or bs.get("id") or 0),
                    md5=str(b.get("checksum") or ""),
                    artist=str(bs.get("artist") or "Unknown"),
                    title=str(bs.get("title") or "Unknown"),
                    diff_name=str(b.get("version") or "Unknown"),
                    mode=_MODE_TO_INT.get(str(b.get("mode") or "osu").lower(), 0),
                    star_rating=float(b.get("difficulty_rating") or 0.0),
                ))
            if not data.get("hasMore"):
                break
            cursor = str(data.get("nextPageCursor") or "")
            if not cursor:
                break
        return out


# ---------------------------------------------------------------------------
# Beatmap mirror downloader
# ---------------------------------------------------------------------------

class BeatmapMirror:
    """Downloads a single .osz from a mirror with retries + fallbacks."""

    def __init__(self, primary: str = DEFAULT_MIRROR,
                 fallbacks: Iterable[str] = FALLBACK_MIRRORS) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.urls = [primary, *fallbacks]

    def download(self, beatmapset_id: int, dest_dir: Path) -> Path | None:
        """Download .osz to dest_dir; return final path or None on failure."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None

        for base_url in self.urls:
            url = f"{base_url}/{beatmapset_id}"
            for attempt in range(HTTP_RETRIES):
                try:
                    with self.session.get(url, stream=True,
                                          timeout=DOWNLOAD_TIMEOUT_S,
                                          allow_redirects=True) as r:
                        if r.status_code == 404:
                            # Beatmap genuinely missing — no point retrying.
                            return None
                        r.raise_for_status()

                        filename = self._filename_from_response(r, beatmapset_id)
                        dest = dest_dir / filename
                        tmp = dest.with_suffix(dest.suffix + ".part")
                        with open(tmp, "wb") as f:
                            for chunk in r.iter_content(chunk_size=64 * 1024):
                                if chunk:
                                    f.write(chunk)
                        tmp.rename(dest)
                        return dest
                except requests.RequestException as e:
                    last_error = e
                    time.sleep(HTTP_BACKOFF_S * (attempt + 1))
                    continue

        # All mirrors + retries exhausted
        if last_error:
            raise last_error
        return None

    @staticmethod
    def _filename_from_response(r: requests.Response, set_id: int) -> str:
        cd = r.headers.get("content-disposition", "")
        m = re.search(r'filename\*?=(?:UTF-\d\'\')?"?([^";]+)"?', cd)
        if m:
            name = m.group(1).strip()
            # Some mirrors URL-encode it
            try:
                from urllib.parse import unquote
                name = unquote(name)
            except Exception:
                pass
            if name.lower().endswith(".osz"):
                return _safe_filename(name)
        return f"{set_id}.osz"


def _safe_filename(name: str) -> str:
    # Strip path separators and other unsafe chars (Windows-friendly).
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "_")
    return name.strip().strip(".") or "beatmap.osz"


# ---------------------------------------------------------------------------
# .osdb file writer  (Collection Manager / osu!collector compatible)
# ---------------------------------------------------------------------------
#
# Format reference: roogue/osu-collector-dl/src/core/OsdbGenerator.ts and
# Piotrekol/CollectionManager OsdbCollectionHandler.cs.
#
# We write "o!dm6" (uncompressed) — this is the format osu-collector-dl
# itself emits, and it loads in Collection Manager and osu!lazer.
#
# Layout (.NET BinaryWriter conventions, little-endian):
#     string  "o!dm6"
#     double  save date in OADate format
#     string  editor (collection uploader name)
#     int32   number of collections (always 1 — one .osdb per collection)
#     string  collection name
#     int32   beatmap count
#     for each beatmap:
#         int32   beatmap id
#         int32   beatmap set id
#         string  artist
#         string  title
#         string  difficulty version
#         string  md5 hash
#         string  user comment ("")
#         byte    mode (0..3)
#         double  star rating
#     int32   number of "hash-only" beatmaps (always 0 here)
#     string  footer "By Piotrekol"
#
# Strings use the .NET BinaryWriter format: a 7-bit-encoded length prefix
# followed by UTF-8 bytes.

class OsdbWriter:
    @staticmethod
    def _write_7bit_int(buf: io.BytesIO, value: int) -> None:
        while value >= 0x80:
            buf.write(bytes([(value & 0x7F) | 0x80]))
            value >>= 7
        buf.write(bytes([value & 0x7F]))

    @classmethod
    def _write_string(cls, buf: io.BytesIO, s: str) -> None:
        data = s.encode("utf-8")
        cls._write_7bit_int(buf, len(data))
        buf.write(data)

    @staticmethod
    def _to_oadate(dt: datetime) -> float:
        # OADate epoch is 1899-12-30. Days since that point as a double.
        epoch = datetime(1899, 12, 30, tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - epoch
        return delta.total_seconds() / 86400.0

    @classmethod
    def write(cls, dest_path: Path, info: CollectionInfo) -> None:
        if not info.beatmaps:
            raise ValueError(
                "OsdbWriter requires per-beatmap details — call "
                "fetch_collection(..., with_beatmap_details=True) first."
            )

        buf = io.BytesIO()
        cls._write_string(buf, "o!dm6")
        buf.write(struct.pack("<d", cls._to_oadate(datetime.now(timezone.utc))))
        cls._write_string(buf, info.uploader or "Unknown")
        buf.write(struct.pack("<i", 1))   # always 1 collection per .osdb

        cls._write_string(buf, info.name or "Unknown")
        buf.write(struct.pack("<i", len(info.beatmaps)))

        for bm in info.beatmaps:
            buf.write(struct.pack("<i", bm.beatmap_id))
            buf.write(struct.pack("<i", bm.set_id))
            cls._write_string(buf, bm.artist or "Unknown")
            cls._write_string(buf, bm.title or "Unknown")
            cls._write_string(buf, bm.diff_name or "Unknown")
            cls._write_string(buf, bm.md5 or "")
            cls._write_string(buf, "")  # user comment
            buf.write(bytes([max(0, min(3, bm.mode))]))
            buf.write(struct.pack("<d", float(bm.star_rating)))

        buf.write(struct.pack("<i", 0))   # no hash-only beatmaps
        cls._write_string(buf, "By Piotrekol")

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(buf.getvalue())


# ---------------------------------------------------------------------------
# osu!lazer auto-importer (cross-platform)
# ---------------------------------------------------------------------------

class OsuLazerImporter:
    """Detect and feed files to a running osu!lazer instance."""

    def __init__(self, binary_override: str | Path | None = None) -> None:
        if binary_override:
            p = Path(binary_override).expanduser()
            self.binary: Path | None = p if p.exists() else None
        else:
            self.binary = self._locate_binary()

    @staticmethod
    def _locate_binary() -> Path | None:
        """Find an osu!lazer executable on disk (best-effort)."""
        candidates: list[Path] = []
        home = Path.home()

        if sys.platform.startswith("linux"):
            # AppImage in common locations
            for d in [home / "Applications", home / "Downloads", home / "bin"]:
                candidates.extend(d.glob("osu*.AppImage"))
            for p in [
                Path("/var/lib/flatpak/exports/bin/sh.ppy.osu"),
                home / ".local/share/flatpak/exports/bin/sh.ppy.osu",
                Path("/usr/bin/osu-lazer"),
                Path("/usr/local/bin/osu-lazer"),
            ]:
                if p.exists():
                    candidates.append(p)
        elif sys.platform == "win32":
            # Squirrel.Windows installer drops it under Local/osulazer
            # with the actual exe inside an "app-X.Y.Z" subfolder.
            base = home / "AppData/Local/osulazer"
            for ver in sorted(base.glob("app-*"), reverse=True):
                p = ver / "osu!.exe"
                if p.exists():
                    candidates.append(p)
            # Direct top-level fallback (rare).
            for p in [
                base / "osu!.exe",
                home / "AppData/Local/Programs/osulazer/osu!.exe",
                Path("C:/Program Files/osulazer/osu!.exe"),
                Path("C:/Program Files (x86)/osulazer/osu!.exe"),
            ]:
                if p.exists():
                    candidates.append(p)
        elif sys.platform == "darwin":
            for p in [
                Path("/Applications/osu!.app/Contents/MacOS/osu!"),
                home / "Applications/osu!.app/Contents/MacOS/osu!",
            ]:
                if p.exists():
                    candidates.append(p)

        return candidates[0] if candidates else None

    def is_running(self) -> bool:
        try:
            import psutil
        except ImportError:
            return False
        for p in psutil.process_iter(attrs=["name"]):
            try:
                name = (p.info.get("name") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if "osu!" in name or "osu.exe" in name or name.startswith("osu_"):
                return True
        return False

    def import_file(self, osz_path: Path) -> bool:
        """Hand a file to the osu!lazer binary; lazer's IPC will pick it up."""
        if not self.binary or not self.binary.exists():
            return False
        try:
            kwargs: dict = dict(
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if sys.platform == "win32":
                # DETACHED_PROCESS so the import call doesn't block on the
                # parent and doesn't pop a console window.
                kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([str(self.binary), str(osz_path)], **kwargs)
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Download worker (background thread)
# ---------------------------------------------------------------------------

@dataclass
class DownloadJob:
    collection_ids: list[int]
    output_dir: Path
    download_beatmaps: bool = True
    generate_osdb: bool = False
    auto_import: bool = True
    osu_binary: str | None = None       # manual override; "" / None = auto
    import_parallel: int = 1            # 1..8 — concurrent import calls
    import_delay_ms: int = 0            # min delay between import calls
    mirror_url: str = DEFAULT_MIRROR


class DownloadWorker(QObject):
    """Performs the actual downloads on a background QThread."""

    log = pyqtSignal(str)
    collection_started = pyqtSignal(int, int, str, int)  # idx, total, name, beatmap_count
    beatmap_progress = pyqtSignal(int, int)              # current, total
    collection_finished = pyqtSignal(int, int, int)      # idx, ok, total
    batch_finished = pyqtSignal(int, int)                # ok_collections, total_collections
    error = pyqtSignal(str)

    def __init__(self, job: DownloadJob) -> None:
        super().__init__()
        self.job = job
        self._cancelled = False
        self.api = OsuCollectorClient()
        self.mirror = BeatmapMirror(primary=job.mirror_url)
        self.importer = (
            OsuLazerImporter(binary_override=job.osu_binary)
            if job.auto_import else None
        )
        # Import throttling state, guarded by a lock so multiple worker
        # threads can share it cleanly.
        import threading as _t
        self._import_lock = _t.Lock()
        self._last_import_ts = 0.0
        self._import_executor: ThreadPoolExecutor | None = None
        if self.importer and self.importer.binary:
            workers = max(1, min(8, job.import_parallel))
            self._import_executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="osu-import",
            )

    def cancel(self) -> None:
        self._cancelled = True
        if self._import_executor:
            self._import_executor.shutdown(wait=False, cancel_futures=True)

    # ---- helpers ----------------------------------------------------------

    def _do_import(self, path: Path) -> None:
        """Run on an import-pool worker; handles delay throttling."""
        if not self.importer or not self.importer.binary:
            return
        if self.job.import_delay_ms > 0:
            with self._import_lock:
                wait = (self._last_import_ts
                        + self.job.import_delay_ms / 1000.0
                        - time.monotonic())
                if wait > 0:
                    time.sleep(wait)
                self._last_import_ts = time.monotonic()
        self.importer.import_file(path)

    def _maybe_import(self, path: Path) -> None:
        """Submit an import job to the pool (non-blocking)."""
        if not self._import_executor:
            return
        try:
            self._import_executor.submit(self._do_import, path)
        except RuntimeError:
            # Pool may have been shut down on cancel.
            pass

    def _download_one(self, set_id: int, col_dir: Path) -> tuple[int, Path | None, str | None]:
        try:
            path = self.mirror.download(set_id, col_dir)
            return set_id, path, None
        except Exception as e:
            return set_id, None, str(e)

    # ---- main loop --------------------------------------------------------

    def run(self) -> None:
        ok_collections = 0
        total = len(self.job.collection_ids)

        for idx, cid in enumerate(self.job.collection_ids, 1):
            if self._cancelled:
                self.log.emit("[cancelled]")
                break

            need_details = self.job.generate_osdb
            try:
                info = self.api.fetch_collection(cid, with_beatmap_details=need_details)
            except Exception as e:
                self.error.emit(f"Collection {cid}: {e}")
                continue

            self.log.emit(
                f"\n=== Collection {idx}/{total}: {info.name} "
                f"by {info.uploader} ({len(info.beatmapset_ids)} sets) ==="
            )
            self.collection_started.emit(idx, total, info.name, len(info.beatmapset_ids))

            safe_name = _safe_filename(info.name)
            col_dir = self.job.output_dir / f"{info.id} - {safe_name}"
            col_dir.mkdir(parents=True, exist_ok=True)

            ok = 0
            failed = 0

            # --- generate .osdb (independent of beatmap downloads) ---
            if self.job.generate_osdb:
                try:
                    osdb_path = col_dir / f"{safe_name}.osdb"
                    OsdbWriter.write(osdb_path, info)
                    self.log.emit(f"  [.osdb] {osdb_path.name}")
                except Exception as e:
                    self.log.emit(f"  [.osdb error] {e}")

            # --- download beatmaps in parallel ---
            if self.job.download_beatmaps and info.beatmapset_ids:
                set_ids = info.beatmapset_ids
                done = 0
                with ThreadPoolExecutor(max_workers=DOWNLOAD_PARALLEL) as ex:
                    futures = {
                        ex.submit(self._download_one, sid, col_dir): sid
                        for sid in set_ids
                    }
                    for fut in as_completed(futures):
                        if self._cancelled:
                            for f in futures:
                                f.cancel()
                            break
                        done += 1
                        self.beatmap_progress.emit(done, len(set_ids))
                        sid, path, err = fut.result()
                        if err:
                            failed += 1
                            self.log.emit(f"  [error {sid}: {err}]")
                            continue
                        if path is None:
                            failed += 1
                            self.log.emit(f"  [skip {sid}: not on mirror]")
                            continue
                        ok += 1
                        self.log.emit(f"  [{done}/{len(set_ids)}] {path.name}")
                        self._maybe_import(path)
            else:
                # No beatmap download requested. Still emit progress so the
                # bar finishes.
                self.beatmap_progress.emit(len(info.beatmapset_ids),
                                           max(len(info.beatmapset_ids), 1))

            self.collection_finished.emit(idx, ok, len(info.beatmapset_ids))
            self.log.emit(f"=== {info.name}: {ok} ok, {failed} failed ===")
            if ok > 0 or self.job.generate_osdb:
                ok_collections += 1

        # Wait for any in-flight imports to drain so the GUI's "done"
        # message reflects reality.
        if self._import_executor:
            self._import_executor.shutdown(wait=True)

        self.batch_finished.emit(ok_collections, total)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setMinimumSize(720, 600)

        self.thread: QThread | None = None
        self.worker: DownloadWorker | None = None
        self.settings = self._load_settings()

        self._build_ui()

    # ----- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        # --- collection IDs ---
        ids_group = QGroupBox("Collections to download")
        ids_layout = QVBoxLayout(ids_group)
        ids_layout.addWidget(QLabel(
            "Paste collection URLs or IDs, one per line:\n"
            "  https://osucollector.com/collections/1833/tech\n"
            "  1838"
        ))
        self.ids_edit = QPlainTextEdit()
        self.ids_edit.setPlaceholderText(
            "1833\nhttps://osucollector.com/collections/44/speed-practice\n…"
        )
        ids_layout.addWidget(self.ids_edit)
        layout.addWidget(ids_group)

        # --- output dir ---
        dir_group = QGroupBox("Output folder")
        dir_layout = QHBoxLayout(dir_group)
        self.dir_edit = QLineEdit(self.settings.get(
            "last_output_dir", str(Path.home() / "Downloads")
        ))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse)
        dir_layout.addWidget(self.dir_edit)
        dir_layout.addWidget(browse)
        layout.addWidget(dir_group)

        # --- what to do ---
        what_group = QGroupBox("What to download")
        what_layout = QVBoxLayout(what_group)

        self.download_beatmaps_cb = QCheckBox("Download beatmap sets (.osz)")
        self.download_beatmaps_cb.setChecked(
            self.settings.get("download_beatmaps", True)
        )
        what_layout.addWidget(self.download_beatmaps_cb)

        self.generate_osdb_cb = QCheckBox(
            "Generate .osdb files (Collection Manager / osu!lazer compatible)"
        )
        self.generate_osdb_cb.setChecked(
            self.settings.get("generate_osdb", True)
        )
        what_layout.addWidget(self.generate_osdb_cb)

        self.auto_import_cb = QCheckBox(
            "Auto-import each beatmap into osu!lazer as it finishes downloading"
        )
        self.auto_import_cb.setChecked(self.settings.get("auto_import", True))
        what_layout.addWidget(self.auto_import_cb)

        self.consolidate_cb = QCheckBox(
            "After downloads finish, move all .osdb files into <output>/db/"
        )
        self.consolidate_cb.setChecked(self.settings.get("consolidate_osdb", True))
        what_layout.addWidget(self.consolidate_cb)
        layout.addWidget(what_group)

        # --- tuning ---
        tune_group = QGroupBox("Tuning")
        tune_form = QFormLayout(tune_group)

        self.import_parallel_spin = QSpinBox()
        self.import_parallel_spin.setRange(1, 8)
        self.import_parallel_spin.setValue(int(self.settings.get("import_parallel", 1)))
        self.import_parallel_spin.setToolTip(
            "How many beatmaps to import into osu!lazer in parallel.\n"
            "1 = strictly one-at-a-time (safest, slowest).\n"
            "Higher = faster but more risk of choking osu!lazer.\n"
            "Beatmap downloads are always 4-parallel — this only affects imports."
        )
        tune_form.addRow("Import parallelism:", self.import_parallel_spin)

        self.import_delay_spin = QSpinBox()
        self.import_delay_spin.setRange(0, 5000)
        self.import_delay_spin.setSuffix(" ms")
        self.import_delay_spin.setSingleStep(50)
        self.import_delay_spin.setValue(int(self.settings.get("import_delay_ms", 200)))
        self.import_delay_spin.setToolTip(
            "Minimum delay between auto-import calls to osu!lazer.\n"
            "Composes with 'Import parallelism' above — parallelism caps\n"
            "burst size, delay caps steady-state rate.\n"
            "Increase this if osu!lazer crashes or chokes during a big batch."
        )
        tune_form.addRow("Import delay:", self.import_delay_spin)

        osu_path_row = QHBoxLayout()
        self.osu_path_edit = QLineEdit(self.settings.get("osu_binary", ""))
        self.osu_path_edit.setPlaceholderText("(auto-detect)")
        osu_browse = QPushButton("Browse…")
        osu_browse.clicked.connect(self._on_browse_osu)
        osu_path_row.addWidget(self.osu_path_edit)
        osu_path_row.addWidget(osu_browse)
        osu_row_w = QWidget()
        osu_row_w.setLayout(osu_path_row)
        tune_form.addRow("osu!lazer binary:", osu_row_w)

        layout.addWidget(tune_group)

        # --- progress ---
        prog_group = QGroupBox("Progress")
        prog_layout = QVBoxLayout(prog_group)
        self.col_label = QLabel("Idle.")
        self.col_progress = QProgressBar()
        self.col_progress.setValue(0)
        self.beatmap_label = QLabel("")
        self.beatmap_progress = QProgressBar()
        self.beatmap_progress.setValue(0)
        prog_layout.addWidget(self.col_label)
        prog_layout.addWidget(self.col_progress)
        prog_layout.addWidget(self.beatmap_label)
        prog_layout.addWidget(self.beatmap_progress)
        layout.addWidget(prog_group)

        # --- log ---
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        layout.addWidget(self.log_view, stretch=1)

        # --- buttons ---
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start download")
        self.start_btn.clicked.connect(self._on_start)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setEnabled(False)
        btn_layout.addStretch()
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.start_btn)
        layout.addLayout(btn_layout)

        self.setCentralWidget(root)

    # ----- settings persistence -------------------------------------------

    def _load_settings(self) -> dict:
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_settings(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({
            "last_output_dir": self.dir_edit.text(),
            "download_beatmaps": self.download_beatmaps_cb.isChecked(),
            "generate_osdb": self.generate_osdb_cb.isChecked(),
            "auto_import": self.auto_import_cb.isChecked(),
            "consolidate_osdb": self.consolidate_cb.isChecked(),
            "import_parallel": self.import_parallel_spin.value(),
            "import_delay_ms": self.import_delay_spin.value(),
            "osu_binary": self.osu_path_edit.text(),
        }, indent=2))

    # ----- event handlers --------------------------------------------------

    def closeEvent(self, event) -> None:    # noqa: N802 (Qt override)
        # Persist settings on window close so adjustments aren't lost
        # if the user closes without clicking Start.
        try:
            self._save_settings()
        except OSError:
            pass
        super().closeEvent(event)

    def _on_browse(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Output folder", self.dir_edit.text()
        )
        if d:
            self.dir_edit.setText(d)

    def _on_browse_osu(self) -> None:
        start = self.osu_path_edit.text() or str(Path.home())
        if sys.platform == "win32":
            filt = "osu! executable (osu!.exe);;All files (*)"
        else:
            filt = "osu! executable (osu*);;All files (*)"
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate osu!lazer binary", start, filt
        )
        if path:
            self.osu_path_edit.setText(path)

    def _on_start(self) -> None:
        ids = self._parse_ids(self.ids_edit.toPlainText())
        if not ids:
            QMessageBox.warning(self, APP_NAME, "No valid collection IDs.")
            return

        out_dir = Path(self.dir_edit.text()).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Can't create output dir:\n{e}")
            return

        self._save_settings()

        if (not self.download_beatmaps_cb.isChecked()
                and not self.generate_osdb_cb.isChecked()):
            QMessageBox.warning(
                self, APP_NAME,
                "Nothing to do — tick at least one of "
                "'Download beatmap sets' or 'Generate .osdb files'."
            )
            return

        job = DownloadJob(
            collection_ids=ids,
            output_dir=out_dir,
            download_beatmaps=self.download_beatmaps_cb.isChecked(),
            generate_osdb=self.generate_osdb_cb.isChecked(),
            auto_import=self.auto_import_cb.isChecked(),
            osu_binary=self.osu_path_edit.text().strip() or None,
            import_parallel=self.import_parallel_spin.value(),
            import_delay_ms=self.import_delay_spin.value(),
        )

        self.thread = QThread()
        self.worker = DownloadWorker(job)
        self.worker.moveToThread(self.thread)

        self.worker.log.connect(self._append_log)
        self.worker.collection_started.connect(self._on_collection_started)
        self.worker.beatmap_progress.connect(self._on_beatmap_progress)
        self.worker.collection_finished.connect(self._on_collection_finished)
        self.worker.batch_finished.connect(self._on_batch_finished)
        self.worker.error.connect(lambda msg: self._append_log(f"ERROR: {msg}"))

        self.thread.started.connect(self.worker.run)
        self.thread.start()

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.col_label.setText("Starting…")
        self.col_progress.setValue(0)
        self.beatmap_label.setText("")
        self.beatmap_progress.setValue(0)

    def _on_cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self._append_log("[cancel requested]")

    @staticmethod
    def _parse_ids(text: str) -> list[int]:
        ids: list[int] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.search(r"/collections/(\d+)", line)
            if m:
                ids.append(int(m.group(1)))
            elif line.isdigit():
                ids.append(int(line))
        return ids

    # ----- worker signal handlers ------------------------------------------

    def _append_log(self, msg: str) -> None:
        self.log_view.appendPlainText(msg)

    def _on_collection_started(self, idx: int, total: int, name: str, n_sets: int) -> None:
        self.col_label.setText(f"Collection {idx}/{total} — {name}  ({n_sets} sets)")
        if total > 0:
            self.col_progress.setMaximum(total)
            self.col_progress.setValue(idx - 1)
        self.beatmap_label.setText("Fetching…")
        self.beatmap_progress.setMaximum(max(n_sets, 1))
        self.beatmap_progress.setValue(0)

    def _on_beatmap_progress(self, current: int, total: int) -> None:
        self.beatmap_progress.setMaximum(max(total, 1))
        self.beatmap_progress.setValue(current)
        self.beatmap_label.setText(f"Beatmap {current} / {total}")

    def _on_collection_finished(self, idx: int, ok: int, total: int) -> None:
        self.col_progress.setValue(idx)

    def _on_batch_finished(self, ok: int, total: int) -> None:
        self._append_log(f"\n[done — {ok}/{total} collections succeeded]")
        self.col_label.setText(f"Done.  {ok}/{total} collections succeeded.")
        if self.consolidate_cb.isChecked():
            self._consolidate_osdb()
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread = None
        self.worker = None
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def _consolidate_osdb(self) -> None:
        out_dir = Path(self.dir_edit.text()).expanduser()
        db_dir = out_dir / "db"
        db_dir.mkdir(exist_ok=True)
        moved = 0
        for f in out_dir.rglob("*.osdb"):
            try:
                if db_dir in f.parents:
                    continue
                dest = db_dir / f.name
                if dest.exists():
                    dest = db_dir / f"{f.parent.name} - {f.name}"
                shutil.move(str(f), str(dest))
                moved += 1
            except OSError:
                continue
        if moved:
            self._append_log(f"[moved {moved} .osdb file(s) into {db_dir}]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
