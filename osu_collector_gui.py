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

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import requests
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
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
MAX_PARALLEL_DOWNLOADS = 4
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
class CollectionInfo:
    id: int
    name: str
    uploader: str
    beatmap_count: int
    beatmapset_ids: list[int] = field(default_factory=list)


class OsuCollectorClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def fetch_collection(self, collection_id: int) -> CollectionInfo:
        """Fetch collection metadata + flat list of beatmapset IDs."""
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

        return CollectionInfo(
            id=int(data["id"]),
            name=str(data.get("name") or f"Collection {collection_id}"),
            uploader=str((data.get("uploader") or {}).get("username") or "?"),
            beatmap_count=int(data.get("beatmapCount") or len(set_ids)),
            beatmapset_ids=set_ids,
        )


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
# osu!lazer auto-importer (cross-platform)
# ---------------------------------------------------------------------------

class OsuLazerImporter:
    """Detect and feed files to a running osu!lazer instance."""

    def __init__(self) -> None:
        self.binary: Path | None = self._locate_binary()

    @staticmethod
    def _locate_binary() -> Path | None:
        """Find an osu!lazer executable on disk (best-effort)."""
        candidates: list[Path] = []
        home = Path.home()

        if sys.platform.startswith("linux"):
            # AppImage in common locations
            for p in (home / "Applications").glob("osu*.AppImage"):
                candidates.append(p)
            # Flatpak shim
            for p in [
                Path("/var/lib/flatpak/exports/bin/sh.ppy.osu"),
                home / ".local/share/flatpak/exports/bin/sh.ppy.osu",
            ]:
                if p.exists():
                    candidates.append(p)
        elif sys.platform == "win32":
            for p in [
                home / "AppData/Local/osulazer/osu!.exe",
                home / "AppData/Local/Programs/osulazer/osu!.exe",
                Path("C:/Program Files/osulazer/osu!.exe"),
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
        """Send a file to the running osu!lazer instance via its IPC."""
        if not self.binary or not self.binary.exists():
            return False
        try:
            subprocess.Popen(
                [str(self.binary), str(osz_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
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
    auto_import: bool
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
        self.importer = OsuLazerImporter() if job.auto_import else None

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        ok_collections = 0
        total = len(self.job.collection_ids)

        for idx, cid in enumerate(self.job.collection_ids, 1):
            if self._cancelled:
                self.log.emit("[cancelled]")
                break
            try:
                info = self.api.fetch_collection(cid)
            except Exception as e:
                self.error.emit(f"Collection {cid}: {e}")
                continue

            self.log.emit(
                f"\n=== Collection {idx}/{total}: {info.name} "
                f"by {info.uploader} ({len(info.beatmapset_ids)} sets) ==="
            )
            self.collection_started.emit(idx, total, info.name, len(info.beatmapset_ids))

            # Per-collection output folder, mirroring osu-collector-dl.
            safe_name = _safe_filename(info.name)
            col_dir = self.job.output_dir / f"{info.id} - {safe_name}"
            col_dir.mkdir(parents=True, exist_ok=True)

            ok = 0
            failed = 0
            for i, set_id in enumerate(info.beatmapset_ids, 1):
                if self._cancelled:
                    break
                self.beatmap_progress.emit(i, len(info.beatmapset_ids))
                try:
                    path = self.mirror.download(set_id, col_dir)
                    if path is None:
                        self.log.emit(f"  [skip {set_id}: not on mirror]")
                        failed += 1
                        continue
                    ok += 1
                    self.log.emit(f"  [{i}/{len(info.beatmapset_ids)}] {path.name}")
                    if self.importer and self.importer.is_running():
                        self.importer.import_file(path)
                except Exception as e:
                    failed += 1
                    self.log.emit(f"  [error {set_id}: {e}]")

            self.collection_finished.emit(idx, ok, len(info.beatmapset_ids))
            self.log.emit(f"=== {info.name}: {ok} ok, {failed} failed ===")
            if ok > 0:
                ok_collections += 1

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

        # --- options ---
        opts_group = QGroupBox("Options")
        opts_layout = QVBoxLayout(opts_group)
        self.auto_import_cb = QCheckBox(
            "Auto-import to osu!lazer as files arrive (if osu!lazer is running)"
        )
        self.auto_import_cb.setChecked(self.settings.get("auto_import", True))
        opts_layout.addWidget(self.auto_import_cb)

        self.consolidate_cb = QCheckBox(
            "Consolidate any generated .osdb files into <output>/db (post-download)"
        )
        self.consolidate_cb.setChecked(self.settings.get("consolidate_osdb", True))
        opts_layout.addWidget(self.consolidate_cb)
        layout.addWidget(opts_group)

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
            "auto_import": self.auto_import_cb.isChecked(),
            "consolidate_osdb": self.consolidate_cb.isChecked(),
        }, indent=2))

    # ----- event handlers --------------------------------------------------

    def _on_browse(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Output folder", self.dir_edit.text()
        )
        if d:
            self.dir_edit.setText(d)

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

        job = DownloadJob(
            collection_ids=ids,
            output_dir=out_dir,
            auto_import=self.auto_import_cb.isChecked(),
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
