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
import shlex
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
APP_VERSION = "0.4.0"
APP_AUTHOR = "Red"
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

CACHE_DIR = Path.home() / (
    ".cache" if sys.platform != "win32" else "AppData/Local"
) / APP_NAME
CM_CLI_CACHE_DIR = CACHE_DIR / "cm-cli"

CM_CLI_RELEASE_URL = (
    "https://github.com/Piotrekol/CollectionManager/releases/latest/"
    "download/CollectionManager-CLI.zip"
)


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
        cls.write_many(dest_path, [info])

    @classmethod
    def write_many(cls, dest_path: Path,
                   collections: list[CollectionInfo],
                   editor: str | None = None) -> None:
        """Write one .osdb file containing one or more collections.

        This is what makes 'merge into a single .osdb then re-import' work
        without needing CM CLI's (nonexistent) merge command.
        """
        buf = io.BytesIO()
        cls._write_string(buf, "o!dm6")
        buf.write(struct.pack("<d", cls._to_oadate(datetime.now(timezone.utc))))
        cls._write_string(buf, editor or (collections[0].uploader if collections else "Unknown"))
        buf.write(struct.pack("<i", len(collections)))

        for info in collections:
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


class OsdbReader:
    """Inverse of OsdbWriter — parses an o!dm6 .osdb file back into
    CollectionInfo dataclasses. Used to round-trip existing lazer
    collections through CM CLI for non-destructive merging.
    """

    @staticmethod
    def _read_7bit_int(buf: io.BytesIO) -> int:
        result = 0
        shift = 0
        for _ in range(5):  # max 5 bytes for a 32-bit int
            b = buf.read(1)
            if not b:
                raise EOFError("unexpected end of .osdb")
            byte = b[0]
            result |= (byte & 0x7F) << shift
            if (byte & 0x80) == 0:
                return result
            shift += 7
        raise ValueError("malformed 7-bit int in .osdb")

    @classmethod
    def _read_string(cls, buf: io.BytesIO) -> str:
        length = cls._read_7bit_int(buf)
        return buf.read(length).decode("utf-8", errors="replace")

    @staticmethod
    def _read_int32(buf: io.BytesIO) -> int:
        return struct.unpack("<i", buf.read(4))[0]

    @staticmethod
    def _read_double(buf: io.BytesIO) -> float:
        return struct.unpack("<d", buf.read(8))[0]

    @staticmethod
    def _read_byte(buf: io.BytesIO) -> int:
        return buf.read(1)[0]

    _SUPPORTED_VERSIONS = {
        "o!dm3": 3, "o!dm4": 4, "o!dm5": 5, "o!dm6": 6,
        "o!dm7": 7, "o!dm8": 8,
    }

    @classmethod
    def read(cls, src_path: Path) -> list[CollectionInfo]:
        raw = src_path.read_bytes()
        buf = io.BytesIO(raw)

        magic = cls._read_string(buf)
        if magic not in cls._SUPPORTED_VERSIONS:
            raise ValueError(
                f"Unsupported .osdb version: {magic!r}. Supported: "
                f"{', '.join(sorted(cls._SUPPORTED_VERSIONS))}"
            )
        version = cls._SUPPORTED_VERSIONS[magic]

        # v7+ wraps the body in a gzip stream produced by SharpCompress's
        # GZipArchive. Python's gzip module reads it fine — the SharpCompress
        # filename header is part of the standard gzip envelope.
        if version >= 7:
            import gzip
            try:
                body = gzip.decompress(raw[buf.tell():])
            except OSError as e:
                raise ValueError(f"gzip decompress failed: {e}") from e
            buf = io.BytesIO(body)
            inner = cls._read_string(buf)
            if inner not in cls._SUPPORTED_VERSIONS:
                raise ValueError(
                    f"Inner version mismatch: expected o!dm*, got {inner!r}"
                )

        cls._read_double(buf)            # save date — ignored
        editor = cls._read_string(buf)
        num_collections = cls._read_int32(buf)

        result: list[CollectionInfo] = []
        for _ in range(num_collections):
            name = cls._read_string(buf)
            online_id = -1
            if version >= 7:
                online_id = cls._read_int32(buf)   # new field in v7+
            n_beatmaps = cls._read_int32(buf)
            beatmaps: list[BeatmapInfo] = []
            for _ in range(n_beatmaps):
                bm_id = cls._read_int32(buf)
                set_id = cls._read_int32(buf) if version >= 2 else 0
                artist = cls._read_string(buf)
                title = cls._read_string(buf)
                diff = cls._read_string(buf)
                md5 = cls._read_string(buf)
                cls._read_string(buf)        # user comment — ignored
                mode = cls._read_byte(buf)
                star = cls._read_double(buf)
                beatmaps.append(BeatmapInfo(
                    beatmap_id=bm_id, set_id=set_id, md5=md5,
                    artist=artist, title=title, diff_name=diff,
                    mode=mode, star_rating=star,
                ))

            # Hash-only beatmaps (md5s without metadata). v3+ wrote this
            # int32 marker; older versions ended the collection here.
            if version >= 3:
                n_hash_only = cls._read_int32(buf)
                for _ in range(n_hash_only):
                    h = cls._read_string(buf)
                    if h:
                        beatmaps.append(BeatmapInfo(
                            beatmap_id=0, set_id=0, md5=h,
                        ))

            result.append(CollectionInfo(
                id=online_id if online_id > 0 else 0,
                name=name,
                uploader=editor,
                beatmap_count=len(beatmaps),
                beatmapset_ids=sorted({b.set_id for b in beatmaps if b.set_id}),
                beatmaps=beatmaps,
            ))

        return result


def merge_collection_lists(
    *lists: list[CollectionInfo],
    on_name_collision: str = "merge",
) -> list[CollectionInfo]:
    """Merge several lists of CollectionInfo into one.

    on_name_collision:
        "merge"  — combine beatmaps from same-named collections (deduped by md5)
        "skip"   — skip the new collection if a name already exists (keep old)
        "rename" — append a numeric suffix to the new collection's name
    """
    by_name: dict[str, CollectionInfo] = {}
    order: list[str] = []

    for lst in lists:
        for c in lst:
            key = c.name.strip()
            if key not in by_name:
                by_name[key] = CollectionInfo(
                    id=c.id, name=c.name, uploader=c.uploader,
                    beatmap_count=len(c.beatmaps),
                    beatmapset_ids=list(c.beatmapset_ids),
                    beatmaps=list(c.beatmaps),
                )
                order.append(key)
                continue

            if on_name_collision == "merge":
                existing = by_name[key]
                seen = {b.md5 for b in existing.beatmaps if b.md5}
                for b in c.beatmaps:
                    if b.md5 and b.md5 not in seen:
                        existing.beatmaps.append(b)
                        seen.add(b.md5)
                existing.beatmap_count = len(existing.beatmaps)
            elif on_name_collision == "skip":
                continue
            elif on_name_collision == "rename":
                n = 2
                new_key = f"{key} ({n})"
                while new_key in by_name:
                    n += 1
                    new_key = f"{key} ({n})"
                renamed = CollectionInfo(
                    id=c.id, name=new_key, uploader=c.uploader,
                    beatmap_count=len(c.beatmaps),
                    beatmapset_ids=list(c.beatmapset_ids),
                    beatmaps=list(c.beatmaps),
                )
                by_name[new_key] = renamed
                order.append(new_key)

    return [by_name[k] for k in order]


# ---------------------------------------------------------------------------
# CollectionManager CLI runner
# ---------------------------------------------------------------------------
#
# We invoke CM CLI to round-trip existing lazer collections through .osdb,
# merge in our new collections in Python, and re-import. CM does NOT have a
# native merge command — its `convert` overwrites — so we do the merging
# ourselves and use CM purely as a Realm <-> .osdb codec.

@dataclass
class CmCliConfig:
    command: list[str]          # full argv prefix to invoke CM CLI
    osu_location: str | None    # passed via -l (auto-detect if None)


class CmCliRunner:
    def __init__(self, cfg: CmCliConfig) -> None:
        self.cfg = cfg

    def export_realm_to_osdb(self, realm_path: Path, dest_osdb: Path) -> None:
        """Read existing lazer collections out to a temp .osdb.

        Always passes -s (SkipOsuLocation). When the input file is itself
        a client.realm, also passing -l would make CM open the same realm
        twice (once via LoadOsuDatabase, once via CollectionLoader),
        which Realm.NET treats as a conflicting open and throws an
        unhandled CLR exception (0xe0434352) under wine.
        """
        argv = [*self.cfg.command, "convert",
                "-i", str(realm_path),
                "-o", str(dest_osdb),
                "-s"]
        self._run(argv)

    def import_osdb_to_realm(self, src_osdb: Path, realm_path: Path) -> None:
        """Overwrite client.realm with the collections from src_osdb.

        Same -s rationale as export. The input is a .osdb so there's no
        double-realm-open risk, but we keep it consistent and avoid
        loading the realm via -l (which would race with our own -o write).
        """
        argv = [*self.cfg.command, "convert",
                "-i", str(src_osdb),
                "-o", str(realm_path),
                "-s"]
        self._run(argv)

    @staticmethod
    def _run(argv: list[str]) -> None:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            # Trim wine's gigantic register dump out of the message —
            # it's noise and pushes the actual .NET exception text off
            # the top of the dialog. Keep just the human-readable lines
            # before "Register dump:" or "Backtrace:".
            stderr = proc.stderr or ""
            for marker in ("Unhandled exception:", "Register dump:", "Backtrace:"):
                idx = stderr.find(marker)
                if idx > 0:
                    stderr = stderr[:idx].rstrip() + (
                        f"\n  [...wine crash dump trimmed; full stderr is "
                        f"{len(proc.stderr)} chars...]"
                    )
                    break
            raise RuntimeError(
                f"CM CLI failed (exit {proc.returncode}):\n"
                f"  cmd: {shlex.join(argv)}\n"
                f"  stdout: {proc.stdout.strip()}\n"
                f"  stderr: {stderr.strip()}"
            )

    @staticmethod
    def autodetect() -> CmCliConfig | None:
        """Best-effort auto-detection of CM CLI on Linux/Windows.

        Search order:
            1. Standard install locations (Squirrel installer, Program Files,
               wine flatpak prefix)
            2. The cache dir populated by CmCliInstaller (auto-downloaded
               from CM's GitHub releases)
        """
        if sys.platform == "win32":
            home = Path.home()
            candidates = [
                home / "AppData/Local/Programs/Collection Manager/CollectionManager.App.Cli.exe",
                Path("C:/Program Files/Collection Manager/CollectionManager.App.Cli.exe"),
                CM_CLI_CACHE_DIR / "CollectionManager.App.Cli.exe",
            ]
            for p in candidates:
                if p.exists():
                    return CmCliConfig(command=[str(p)], osu_location=None)
            return None

        # Linux: try the wine flatpak install first (the most common
        # setup), then the auto-downloaded cache running through wine.
        wine_exe = (
            Path.home()
            / ".var/app/org.winehq.Wine/data/wine/drive_c/users"
            / os.environ.get("USER", "red")
            / "AppData/Local/Programs/Collection Manager/CollectionManager.App.Cli.exe"
        )
        if wine_exe.exists():
            win_path = (
                "C:\\users\\"
                + os.environ.get("USER", "red")
                + "\\AppData\\Local\\Programs\\Collection Manager"
                + "\\CollectionManager.App.Cli.exe"
            )
            return CmCliConfig(
                command=["flatpak", "run", "org.winehq.Wine", win_path],
                osu_location=None,
            )

        # Auto-downloaded copy in our cache dir, run via wine flatpak
        # (wine needs filesystem permission for the cache dir — we grant
        # it once during install).
        cached = CM_CLI_CACHE_DIR / "CollectionManager.App.Cli.exe"
        if cached.exists() and shutil.which("flatpak"):
            # Wine flatpak prefers Z: drive paths for unix files.
            wine_path = "Z:" + str(cached).replace("/", "\\")
            return CmCliConfig(
                command=["flatpak", "run", "org.winehq.Wine", wine_path],
                osu_location=None,
            )

        # Last-ditch: native build on a system that has one.
        for p in (Path("/usr/local/bin/CollectionManager.App.Cli"),
                  Path("/usr/bin/CollectionManager.App.Cli")):
            if p.exists():
                return CmCliConfig(command=[str(p)], osu_location=None)
        return None


# ---------------------------------------------------------------------------
# Auto-installer for Collection Manager CLI
# ---------------------------------------------------------------------------

class CmCliInstaller:
    """Downloads CM CLI from its GitHub releases into our cache dir.

    The release zip is small (~3.6 MB) and self-contained — a single
    .exe + a native realm-wrappers.dll. Works natively on Windows and
    via wine on Linux.
    """

    @staticmethod
    def installed_exe() -> Path | None:
        exe = CM_CLI_CACHE_DIR / "CollectionManager.App.Cli.exe"
        return exe if exe.exists() else None

    @staticmethod
    def install(log_func=print) -> Path:
        """Download + extract latest CM CLI release. Returns the .exe path."""
        import urllib.request
        import zipfile

        CM_CLI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        log_func(f"Downloading {CM_CLI_RELEASE_URL}...")

        req = urllib.request.Request(
            CM_CLI_RELEASE_URL, headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        log_func(f"Downloaded {len(data) // 1024} KiB, extracting...")

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(CM_CLI_CACHE_DIR)

        exe = CM_CLI_CACHE_DIR / "CollectionManager.App.Cli.exe"
        if not exe.exists():
            raise RuntimeError(
                "CM CLI zip extracted but expected exe not found at "
                f"{exe}. The release archive layout may have changed."
            )

        # On Linux, grant the wine flatpak permission to read the cache
        # directory so it can actually find the exe. This is a no-op if
        # the override is already set, and harmless if wine isn't installed.
        if sys.platform != "win32" and shutil.which("flatpak"):
            try:
                subprocess.run(
                    ["flatpak", "override", "--user",
                     f"--filesystem={CM_CLI_CACHE_DIR}",
                     "org.winehq.Wine"],
                    check=False, capture_output=True, timeout=10,
                )
                log_func(f"Granted wine flatpak access to {CM_CLI_CACHE_DIR}")
            except (OSError, subprocess.SubprocessError):
                pass

        log_func(f"Installed CM CLI: {exe}")
        return exe


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
    # Lazer collection merging via CM CLI
    add_to_lazer_collections: bool = False
    cm_cli_command: list[str] | None = None  # full argv prefix
    lazer_realm_path: str | None = None      # path to client.realm
    target_collection_name: str | None = None  # if set, all maps go here
    restart_lazer_after: bool = False


class DownloadWorker(QObject):
    """Performs the actual downloads on a background QThread."""

    log = pyqtSignal(str)
    collection_started = pyqtSignal(int, int, str, int)  # idx, total, name, beatmap_count
    beatmap_progress = pyqtSignal(int, int)              # current, total
    collection_finished = pyqtSignal(int, int, int)      # idx, ok, total
    batch_finished = pyqtSignal(int, int)                # ok_collections, total_collections
    error = pyqtSignal(str)
    # Asks the GUI thread to put up a "did imports finish?" dialog and
    # wait for the user before we proceed with the destructive merge.
    # Payload is the number of import calls we issued during the batch.
    awaiting_import_confirmation = pyqtSignal(int)

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
        self._import_calls_issued = 0
        # Event used to pause the worker before the destructive merge so
        # the user can confirm osu!lazer has finished its async import
        # queue. Set by confirm_merge_continue() from the GUI thread.
        self._continue_merge_event = _t.Event()
        if self.importer and self.importer.binary:
            workers = max(1, min(8, job.import_parallel))
            self._import_executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="osu-import",
            )

    def cancel(self) -> None:
        self._cancelled = True
        # Unblock any thread waiting on the merge confirmation gate so
        # it can notice the cancel and exit cleanly.
        self._continue_merge_event.set()
        if self._import_executor:
            self._import_executor.shutdown(wait=False, cancel_futures=True)

    def confirm_merge_continue(self) -> None:
        """Called from the GUI thread when the user clicks OK on the
        'did osu!lazer finish importing?' dialog. Releases the worker
        from its wait at the start of _merge_into_lazer."""
        self._continue_merge_event.set()

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
            self._import_calls_issued += 1
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

        # --- merge into lazer collections via CM CLI ---
        if self.job.add_to_lazer_collections and not self._cancelled:
            try:
                self._merge_into_lazer()
            except Exception as e:
                self.error.emit(f"lazer collection merge failed: {e}")

        self.batch_finished.emit(ok_collections, total)

    # ---- lazer collection merge ------------------------------------------

    def _merge_into_lazer(self) -> None:
        if not self.job.cm_cli_command:
            raise RuntimeError(
                "Collection Manager CLI not configured. Set its path in "
                "the GUI's 'Lazer collections' section."
            )

        # If we issued any auto-import calls, lazer is doing async work
        # in the background — extracting .osz files, hashing beatmaps,
        # writing to client.realm. We must wait until that's done, or
        # the merge will kill lazer mid-import and lose data. Lazer
        # doesn't expose a "queue empty" signal, so we ask the user.
        if self._import_calls_issued > 0:
            self.log.emit(
                f"\n[lazer] {self._import_calls_issued} auto-import call(s) "
                "were issued. Waiting for user confirmation that osu!lazer "
                "has finished importing them before touching client.realm..."
            )
            self._continue_merge_event.clear()
            self.awaiting_import_confirmation.emit(self._import_calls_issued)
            # Long but bounded wait — 1h cap so a forgotten dialog
            # doesn't leak the worker thread forever.
            self._continue_merge_event.wait(timeout=3600)
            if self._cancelled:
                self.log.emit("[lazer] cancelled while waiting for import "
                              "confirmation")
                return
            self.log.emit("[lazer] user confirmed; proceeding with merge")
        if not self.job.lazer_realm_path:
            raise RuntimeError("lazer client.realm path not configured.")
        realm_path = Path(self.job.lazer_realm_path).expanduser()
        if not realm_path.exists():
            raise FileNotFoundError(f"client.realm not found at {realm_path}")

        cm = CmCliRunner(CmCliConfig(
            command=list(self.job.cm_cli_command),
            osu_location=None,
        ))

        # Read the existing collections via a SNAPSHOT copy, so lazer
        # can stay open for now. Killing lazer is deferred to the
        # write-back phase. (Realm is MVCC — a file-level copy gives
        # us a consistent point-in-time snapshot of committed state.)
        was_running = False  # filled in later, before the write-back

        # Same wine-sandbox constraint as _fetch_existing_collections —
        # CM CLI can only write to paths the wine flatpak can see, which
        # in practice means the realm's own parent dir.
        self.log.emit("\n[lazer] snapshotting realm and exporting existing collections...")
        tmp_dir = realm_path.parent / ".oc-gui-tmp"
        tmp_dir.mkdir(exist_ok=True)
        snapshot_realm = tmp_dir / "snapshot.realm"
        existing_osdb = tmp_dir / "existing.osdb"

        try:
            shutil.copy2(realm_path, snapshot_realm)
        except OSError as e:
            raise RuntimeError(
                f"Couldn't snapshot client.realm to {snapshot_realm}: {e}"
            ) from e

        # FAIL-CLOSED: if we can't read the existing collections, ABORT.
        # Treating "couldn't read" as "you have no collections" would
        # cause CM CLI's destructive Write to nuke the realm — exactly
        # what happened in v0.4.0. Refuse to proceed instead.
        try:
            cm.export_realm_to_osdb(snapshot_realm, existing_osdb)
        except Exception as e:
            raise RuntimeError(
                "CM CLI failed to export your existing lazer collections "
                "from client.realm. Aborting before any destructive write — "
                "your collections are untouched.\n\nUnderlying error: "
                f"{e}"
            ) from e

        if not existing_osdb.exists() or existing_osdb.stat().st_size == 0:
            raise RuntimeError(
                "CM CLI exported a 0-byte or missing file when reading "
                "your existing lazer collections. Aborting before any "
                "destructive write — your collections are untouched.\n\n"
                f"Expected file: {existing_osdb}"
            )

        try:
            existing = OsdbReader.read(existing_osdb)
        except Exception as e:
            raise RuntimeError(
                "Couldn't parse the .osdb that CM CLI exported from your "
                "realm. Aborting before any destructive write — your "
                "collections are untouched.\n\nParser error: "
                f"{e}\nFile: {existing_osdb} ({existing_osdb.stat().st_size} bytes)"
            ) from e

        self.log.emit(f"[lazer] {len(existing)} existing collection(s) loaded")

        # Belt-and-braces sanity check: if the realm is non-trivial in size
        # but we got back zero collections, something is wrong — refuse to
        # write rather than risk wiping a collection database that just
        # happened to parse weirdly.
        realm_size = realm_path.stat().st_size
        if not existing and realm_size > 1024 * 1024:
            raise RuntimeError(
                f"client.realm is {realm_size // 1024 // 1024} MB but the "
                "export contained zero collections. This usually means the "
                "export tool is incompatible with your lazer/realm version. "
                "Aborting before any destructive write — your collections "
                "are untouched."
            )

        # Collect every .osdb we generated this run.
        new_collections: list[CollectionInfo] = []
        for f in Path(self.job.output_dir).rglob("*.osdb"):
            # Skip our own temp dir
            if tmp_dir in f.parents:
                continue
            try:
                new_collections.extend(OsdbReader.read(f))
            except Exception as e:
                self.log.emit(f"[lazer] skip unreadable {f.name}: {e}")

        if not new_collections:
            self.log.emit("[lazer] no new collections to merge — skipping")
            return

        # If the user picked a single target collection name, rewrite ALL
        # the new collections to use that name. The merge step will then
        # combine them (and any same-named existing one) into a single
        # collection.
        if self.job.target_collection_name:
            target = self.job.target_collection_name
            self.log.emit(
                f"[lazer] funneling all new maps into collection {target!r}"
            )
            for c in new_collections:
                c.name = target

        merged = merge_collection_lists(
            existing, new_collections,
            on_name_collision="merge",
        )
        self.log.emit(
            f"[lazer] merged result: {len(merged)} collection(s) "
            f"(existing {len(existing)} + new {len(new_collections)} → {len(merged)})"
        )

        merged_osdb = tmp_dir / "merged.osdb"
        OsdbWriter.write_many(merged_osdb, merged, editor="osu-collector-gui")

        # Backup the realm before overwriting — paranoia is justified here.
        backup = realm_path.with_suffix(
            realm_path.suffix + f".bak-{int(time.time())}"
        )
        try:
            shutil.copy2(realm_path, backup)
            self.log.emit(f"[lazer] backed up realm to {backup.name}")
        except OSError as e:
            self.log.emit(f"[lazer] WARNING: couldn't back up realm: {e}")

        # Lazer must NOT be running while CM rewrites client.realm. We
        # already killed it at the start of this method; this second
        # check catches any auto-restart that may have happened in the
        # meantime (e.g. some launchers respawn it).
        if self._lazer_kill_if_running():
            was_running = True
        try:
            self.log.emit("[lazer] writing merged collections back to realm...")
            cm.import_osdb_to_realm(merged_osdb, realm_path)
            self.log.emit("[lazer] done.")
        finally:
            try:
                shutil.rmtree(tmp_dir)
            except OSError:
                pass

        if self.job.restart_lazer_after or was_running:
            self._lazer_relaunch()

    def _lazer_kill_if_running(self) -> bool:
        try:
            import psutil
        except ImportError:
            return False

        targets: list = []
        for p in psutil.process_iter(attrs=["name", "exe"]):
            try:
                name = (p.info.get("name") or "").lower()
                exe = (p.info.get("exe") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if ("osu!" in name or "osu.exe" in name
                    or name.startswith("osu_")
                    or "osu!.exe" in exe):
                targets.append(p)

        if not targets:
            return False

        self.log.emit(
            f"[lazer] terminating {len(targets)} osu!lazer process(es) "
            "and waiting for them to exit cleanly..."
        )
        # SIGTERM first — lazer's signal handler flushes the Realm and
        # closes its file handles. Skipping the wait or going straight
        # to SIGKILL leaves the realm in a half-flushed state that
        # Realm.NET refuses to re-open under wine, crashing CM CLI with
        # 0xe0434352.
        for p in targets:
            try:
                p.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Wait up to 15 s for graceful shutdown.
        gone, alive = psutil.wait_procs(targets, timeout=15)
        for p in alive:
            self.log.emit(f"[lazer] PID {p.pid} ignored SIGTERM, sending SIGKILL")
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=5)

        # Extra grace period so the kernel releases the file handles
        # and any Realm .lock entries are visible-as-stale to the next
        # opener.
        time.sleep(2)

        # Clean up obviously-stale Realm lock + management files. Lazer
        # leaves these behind when it exits cleanly too, but a hard
        # SIGKILL after a hung shutdown can produce a corrupt .lock that
        # Realm.NET refuses. Removing them is safe — Realm will recreate
        # on next open.
        try:
            realm_path = Path(self.job.lazer_realm_path).expanduser()
            for stale in [
                realm_path.parent / "client.realm.lock",
                realm_path.parent / "client.realm.note",
            ]:
                if stale.exists():
                    try:
                        stale.unlink()
                        self.log.emit(f"[lazer] removed stale {stale.name}")
                    except OSError:
                        pass
        except (TypeError, OSError):
            pass

        self.log.emit("[lazer] all instances stopped")
        return True

    def _lazer_relaunch(self) -> None:
        if not self.importer or not self.importer.binary:
            self.log.emit("[lazer] no binary path configured — not relaunching")
            return
        try:
            kwargs: dict = dict(stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([str(self.importer.binary)], **kwargs)
            self.log.emit("[lazer] relaunched osu!lazer")
        except OSError as e:
            self.log.emit(f"[lazer] relaunch failed: {e}")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} by {APP_AUTHOR}")
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

        # --- lazer collection merging via Collection Manager CLI ---
        lazer_group = QGroupBox("Add downloaded maps to osu!lazer collections")
        lazer_form = QFormLayout(lazer_group)

        self.add_to_lazer_cb = QCheckBox(
            "Merge generated .osdb files into osu!lazer's collection database"
        )
        self.add_to_lazer_cb.setChecked(
            self.settings.get("add_to_lazer_collections", False)
        )
        self.add_to_lazer_cb.setToolTip(
            "Uses Collection Manager CLI to round-trip your existing\n"
            "lazer collections + the new ones into a merged client.realm.\n"
            "Requires Collection Manager installed.\n"
            "osu!lazer will be terminated during the merge and (optionally)\n"
            "relaunched afterwards."
        )
        lazer_form.addRow(self.add_to_lazer_cb)

        cm_row = QHBoxLayout()
        # cm_cli_command is stored as a list in settings (canonical), but
        # the QLineEdit shows a shell-quoted string for editing convenience.
        # Legacy: an older version stored it as a raw space-joined string,
        # which broke for paths with spaces. We silently discard those —
        # the user just needs to click Auto-detect once.
        _saved_cmd = self.settings.get("cm_cli_command", [])
        if isinstance(_saved_cmd, list) and _saved_cmd:
            _saved_cmd_text = shlex.join(_saved_cmd)
        else:
            _saved_cmd_text = ""
        self.cm_cli_edit = QLineEdit(_saved_cmd_text)
        self.cm_cli_edit.setPlaceholderText(
            "(auto-detect: wine flatpak or native CM CLI)"
        )
        cm_detect = QPushButton("Auto-detect")
        cm_detect.clicked.connect(self._on_detect_cm)
        cm_row.addWidget(self.cm_cli_edit)
        cm_row.addWidget(cm_detect)
        cm_w = QWidget(); cm_w.setLayout(cm_row)
        lazer_form.addRow("CM CLI command:", cm_w)

        realm_row = QHBoxLayout()
        self.realm_edit = QLineEdit(self.settings.get(
            "lazer_realm_path",
            str(Path.home() / ".local/share/osu/client.realm"),
        ))
        realm_browse = QPushButton("Browse…")
        realm_browse.clicked.connect(self._on_browse_realm)
        realm_row.addWidget(self.realm_edit)
        realm_row.addWidget(realm_browse)
        realm_w = QWidget(); realm_w.setLayout(realm_row)
        lazer_form.addRow("client.realm:", realm_w)

        # Target picker: existing collections fetched via CM CLI, or
        # "create new", or the default "one per osu!collector collection".
        from PyQt6.QtWidgets import QComboBox
        target_row = QHBoxLayout()
        self.target_combo = QComboBox()
        self.target_combo.setEditable(False)
        self.target_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._reset_target_combo()
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        self.refresh_collections_btn = QPushButton("Refresh")
        self.refresh_collections_btn.setToolTip(
            "Fetch the list of existing osu!lazer collections by running\n"
            "Collection Manager CLI against your client.realm."
        )
        self.refresh_collections_btn.clicked.connect(self._on_refresh_collections)
        target_row.addWidget(self.target_combo, stretch=1)
        target_row.addWidget(self.refresh_collections_btn)
        target_w = QWidget(); target_w.setLayout(target_row)
        lazer_form.addRow("Add maps to:", target_w)

        # New-collection name field, only shown when "Create new..." picked.
        self.new_name_edit = QLineEdit()
        self.new_name_edit.setPlaceholderText("Name of the new collection")
        self.new_name_edit.setText(self.settings.get("new_collection_name", ""))
        self.new_name_edit.setVisible(False)
        lazer_form.addRow("New collection name:", self.new_name_edit)
        # Track the row's label widget so we can show/hide it together.
        self._new_name_label = lazer_form.labelForField(self.new_name_edit)
        if self._new_name_label is not None:
            self._new_name_label.setVisible(False)

        self.restart_lazer_cb = QCheckBox(
            "Restart osu!lazer after merging (otherwise just leave it closed)"
        )
        self.restart_lazer_cb.setChecked(
            self.settings.get("restart_lazer_after", True)
        )
        lazer_form.addRow(self.restart_lazer_cb)

        # Recovery row — restore client.realm from a previous backup.
        recover_row = QHBoxLayout()
        self.recover_btn = QPushButton("Recover realm from backup…")
        self.recover_btn.setToolTip(
            "Restore client.realm from a .bak-<timestamp> snapshot taken\n"
            "before a previous merge. osu!lazer will be terminated first\n"
            "and the current realm itself will also be backed up before\n"
            "the restore, so this action is reversible."
        )
        self.recover_btn.clicked.connect(self._on_recover_realm)
        recover_row.addStretch()
        recover_row.addWidget(self.recover_btn)
        lazer_form.addRow(recover_row)

        layout.addWidget(lazer_group)

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
        # Persist cm_cli_command as a list, never a string. The QLineEdit
        # text is parsed once on save so the canonical form on disk is
        # quote-safe.
        cm_text = self.cm_cli_edit.text().strip()
        cm_list: list[str] = []
        if cm_text:
            try:
                cm_list = shlex.split(cm_text)
            except ValueError:
                cm_list = cm_text.split()
        CONFIG_FILE.write_text(json.dumps({
            "last_output_dir": self.dir_edit.text(),
            "download_beatmaps": self.download_beatmaps_cb.isChecked(),
            "generate_osdb": self.generate_osdb_cb.isChecked(),
            "auto_import": self.auto_import_cb.isChecked(),
            "consolidate_osdb": self.consolidate_cb.isChecked(),
            "import_parallel": self.import_parallel_spin.value(),
            "import_delay_ms": self.import_delay_spin.value(),
            "osu_binary": self.osu_path_edit.text(),
            "add_to_lazer_collections": self.add_to_lazer_cb.isChecked(),
            "cm_cli_command": cm_list,
            "lazer_realm_path": self.realm_edit.text(),
            "target_collection": self.target_combo.currentText(),
            "new_collection_name": self.new_name_edit.text(),
            "restart_lazer_after": self.restart_lazer_cb.isChecked(),
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

    def _on_detect_cm(self) -> None:
        cfg = CmCliRunner.autodetect()
        if cfg is not None:
            self.cm_cli_edit.setText(shlex.join(cfg.command))
            return

        # Nothing found locally — offer to download from GitHub releases.
        ans = QMessageBox.question(
            self, APP_NAME,
            "Collection Manager CLI was not found in any standard "
            "location.\n\n"
            "Download the latest release (~4 MB) from "
            "github.com/Piotrekol/CollectionManager into "
            f"{CM_CLI_CACHE_DIR} ?\n\n"
            "On Linux this also runs:\n"
            f"  flatpak override --user --filesystem={CM_CLI_CACHE_DIR} org.winehq.Wine\n"
            "so the wine sandbox can read it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        QApplication.processEvents()
        try:
            CmCliInstaller.install(log_func=lambda s: None)
        except Exception as e:
            QMessageBox.critical(
                self, APP_NAME,
                f"Failed to install Collection Manager CLI:\n\n{e}"
            )
            return

        cfg = CmCliRunner.autodetect()
        if cfg is None:
            QMessageBox.warning(
                self, APP_NAME,
                "Install completed but the auto-detector still can't find "
                f"CM CLI in {CM_CLI_CACHE_DIR}. Open an issue with this "
                "message."
            )
            return
        self.cm_cli_edit.setText(shlex.join(cfg.command))
        QMessageBox.information(
            self, APP_NAME,
            f"Installed Collection Manager CLI to {CM_CLI_CACHE_DIR}."
        )

    def _on_browse_realm(self) -> None:
        start = self.realm_edit.text() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate client.realm", start,
            "Realm DB (client.realm);;All files (*)"
        )
        if path:
            self.realm_edit.setText(path)

    # ----- shared lazer process helpers (used by refresh + recover) -------

    @staticmethod
    def _lazer_is_running() -> bool:
        try:
            import psutil
        except ImportError:
            return False
        for p in psutil.process_iter(attrs=["name", "exe"]):
            try:
                name = (p.info.get("name") or "").lower()
                exe = (p.info.get("exe") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if ("osu!" in name or "osu.exe" in name
                    or name.startswith("osu_")
                    or "osu!.exe" in exe):
                return True
        return False

    @staticmethod
    def _lazer_kill_running() -> int:
        """Terminate any running osu!lazer process. Returns count killed."""
        try:
            import psutil
        except ImportError:
            return 0
        killed = 0
        for p in psutil.process_iter(attrs=["name", "exe"]):
            try:
                name = (p.info.get("name") or "").lower()
                exe = (p.info.get("exe") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if ("osu!" in name or "osu.exe" in name
                    or name.startswith("osu_")
                    or "osu!.exe" in exe):
                try:
                    p.terminate()
                    killed += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        return killed

    # ----- target collection picker ---------------------------------------

    DEFAULT_TARGET = "(default — one lazer collection per osu!collector collection)"
    NEW_TARGET = "+ Create new collection..."
    SEPARATOR = "──────────"

    def _reset_target_combo(self) -> None:
        """Populate the target combo with just the default + 'Create new'."""
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItem(self.DEFAULT_TARGET)
        self.target_combo.addItem(self.NEW_TARGET)
        # Restore last-used selection if it still makes sense.
        saved = self.settings.get("target_collection", "")
        idx = self.target_combo.findText(saved) if saved else 0
        self.target_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.target_combo.blockSignals(False)

    def _on_target_changed(self, _idx: int) -> None:
        text = self.target_combo.currentText()
        show_new = (text == self.NEW_TARGET)
        self.new_name_edit.setVisible(show_new)
        if self._new_name_label is not None:
            self._new_name_label.setVisible(show_new)
        if show_new:
            self.new_name_edit.setFocus()

    def _resolve_cm_cli(self) -> list[str] | None:
        raw = self.cm_cli_edit.text().strip()
        if raw:
            try:
                return shlex.split(raw)
            except ValueError:
                # Mismatched quotes etc. — fall back to plain split.
                return raw.split()
        cfg = CmCliRunner.autodetect()
        if cfg is not None:
            self.cm_cli_edit.setText(shlex.join(cfg.command))
            return cfg.command
        return None

    def _on_refresh_collections(self) -> None:
        """Run CM CLI export to read existing lazer collection names."""
        realm_str = self.realm_edit.text().strip()
        if not realm_str or not Path(realm_str).expanduser().exists():
            QMessageBox.warning(
                self, APP_NAME,
                "client.realm path is empty or doesn't exist. Set it first."
            )
            return
        cmd = self._resolve_cm_cli()
        if cmd is None:
            QMessageBox.warning(
                self, APP_NAME,
                "Collection Manager CLI not found. Install it or paste the "
                "full invocation into the CM CLI command field."
            )
            return

        self.refresh_collections_btn.setEnabled(False)
        self.refresh_collections_btn.setText("Working…")
        QApplication.processEvents()
        try:
            collections = self._fetch_existing_collections(
                cmd, Path(realm_str).expanduser()
            )
        except Exception as e:
            QMessageBox.critical(
                self, APP_NAME,
                f"Failed to read existing collections:\n\n{e}"
            )
            self.refresh_collections_btn.setEnabled(True)
            self.refresh_collections_btn.setText("Refresh")
            return
        self.refresh_collections_btn.setEnabled(True)
        self.refresh_collections_btn.setText("Refresh")

        # Rebuild the combo: default → existing items → separator → new.
        previous = self.target_combo.currentText()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItem(self.DEFAULT_TARGET)
        if collections:
            self.target_combo.insertSeparator(self.target_combo.count())
            for c in collections:
                label = f"{c.name}  ({len(c.beatmaps)} maps)"
                self.target_combo.addItem(label, userData=c.name)
        self.target_combo.insertSeparator(self.target_combo.count())
        self.target_combo.addItem(self.NEW_TARGET)
        # Try to restore previous selection
        idx = self.target_combo.findText(previous)
        self.target_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.target_combo.blockSignals(False)
        self._on_target_changed(self.target_combo.currentIndex())

        QMessageBox.information(
            self, APP_NAME,
            f"Found {len(collections)} existing collection(s) in lazer.\n"
            "They're now selectable in the 'Add maps to' dropdown."
        )

    def _fetch_existing_collections(
        self, cm_cli_command: list[str], realm_path: Path,
    ) -> list[CollectionInfo]:
        """Run CM CLI to export client.realm to a temp .osdb and parse it.

        Strategy: copy the live client.realm to a sibling snapshot file,
        then export from the snapshot. This lets osu!lazer stay open
        (Realm uses MVCC so a file copy is a consistent point-in-time
        snapshot of the committed state) and avoids the
        Realm.NET 'realm in use' crash that fires when CM CLI tries to
        open a file that another process already holds the writer lock
        on.

        IMPORTANT: when CM CLI runs through the wine flatpak, the
        sandbox can only see directories explicitly granted to it
        (typically just ~/.local/share/osu). Temp files in /tmp are
        silently dropped — wine can't write there. So we keep the
        snapshot + .osdb output next to the original realm.
        """
        snapshot = realm_path.parent / f".oc-gui-snapshot-{os.getpid()}.realm"
        out = realm_path.parent / f".oc-gui-export-{os.getpid()}.osdb"
        try:
            shutil.copy2(realm_path, snapshot)

            cm = CmCliRunner(CmCliConfig(
                command=list(cm_cli_command),
                osu_location=None,
            ))
            cm.export_realm_to_osdb(snapshot, out)
            if not out.exists() or out.stat().st_size == 0:
                raise RuntimeError(
                    "Collection Manager CLI exited without producing an "
                    "output file. Most likely the wine flatpak sandbox "
                    "couldn't write to:\n"
                    f"  {out}\n"
                    "Grant it explicitly with:\n"
                    f"  flatpak override --user --filesystem={out.parent} org.winehq.Wine"
                )
            return OsdbReader.read(out)
        finally:
            for p in (snapshot, out):
                try:
                    p.unlink()
                except OSError:
                    pass

    # ----- recover realm ---------------------------------------------------

    def _on_recover_realm(self) -> None:
        """Restore client.realm from a .bak-<timestamp> snapshot."""
        realm_str = self.realm_edit.text().strip()
        if not realm_str:
            QMessageBox.warning(self, APP_NAME,
                                "Set the client.realm path first.")
            return
        realm = Path(realm_str).expanduser()
        if not realm.parent.exists():
            QMessageBox.critical(
                self, APP_NAME,
                f"Directory does not exist:\n{realm.parent}"
            )
            return

        # List available backups (any file matching client.realm.bak-*).
        backups = sorted(
            realm.parent.glob(realm.name + ".bak-*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not backups:
            QMessageBox.information(
                self, APP_NAME,
                f"No backups found in {realm.parent}.\n\n"
                f"Backups are created automatically (named "
                f"'{realm.name}.bak-<timestamp>') the first time you merge "
                "collections into lazer. None exist yet."
            )
            return

        # Build a friendly picker with timestamps + sizes.
        items: list[str] = []
        for b in backups:
            ts = datetime.fromtimestamp(b.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            mb = b.stat().st_size / (1024 * 1024)
            items.append(f"{b.name}    ({ts}, {mb:.1f} MB)")
        items.append("Browse for a file…")

        from PyQt6.QtWidgets import QInputDialog
        choice, ok = QInputDialog.getItem(
            self, "Recover realm",
            f"Pick a backup to restore over:\n{realm}",
            items, 0, False,
        )
        if not ok or not choice:
            return

        if choice == "Browse for a file…":
            path, _ = QFileDialog.getOpenFileName(
                self, "Pick backup realm", str(realm.parent),
                "Realm backup (*.bak-* *.realm);;All files (*)"
            )
            if not path:
                return
            chosen = Path(path)
        else:
            chosen = backups[items.index(choice)]

        if not chosen.exists():
            QMessageBox.critical(self, APP_NAME, f"Backup file gone: {chosen}")
            return

        # Confirm with full details.
        confirm = QMessageBox.question(
            self, APP_NAME,
            f"Restore client.realm from this backup?\n\n"
            f"  source: {chosen}\n"
            f"  target: {realm}\n\n"
            f"osu!lazer will be terminated first. The current realm will "
            f"itself be backed up to {realm.name}.before-recover-<timestamp> "
            f"so you can undo.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # Kill any running lazer.
        try:
            import psutil
            for p in psutil.process_iter(attrs=["name", "exe"]):
                try:
                    name = (p.info.get("name") or "").lower()
                    exe = (p.info.get("exe") or "").lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if ("osu!" in name or "osu.exe" in name
                        or name.startswith("osu_")
                        or "osu!.exe" in exe):
                    try:
                        p.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            time.sleep(2)
        except ImportError:
            pass

        # Take a safety copy of the current state before overwriting.
        try:
            if realm.exists():
                ts = int(time.time())
                safety = realm.with_suffix(realm.suffix + f".before-recover-{ts}")
                shutil.copy2(realm, safety)
        except OSError as e:
            r = QMessageBox.warning(
                self, APP_NAME,
                f"Couldn't back up the current realm before restoring:\n{e}\n\n"
                "Continue anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        # The actual restore.
        try:
            shutil.copy2(chosen, realm)
        except OSError as e:
            QMessageBox.critical(
                self, APP_NAME,
                f"Restore failed:\n{e}"
            )
            return

        QMessageBox.information(
            self, APP_NAME,
            f"Restored {realm.name} from {chosen.name}.\n\n"
            "You can launch osu!lazer normally now."
        )

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

        # Validate the lazer-merge config if the user enabled it.
        add_to_lazer = self.add_to_lazer_cb.isChecked()
        cm_cli_cmd: list[str] | None = None
        if add_to_lazer:
            if not self.generate_osdb_cb.isChecked():
                QMessageBox.warning(
                    self, APP_NAME,
                    "Adding to osu!lazer collections requires .osdb generation. "
                    "Tick 'Generate .osdb files' too."
                )
                return
            raw = self.cm_cli_edit.text().strip()
            if not raw:
                cfg = CmCliRunner.autodetect()
                if cfg is None:
                    QMessageBox.warning(
                        self, APP_NAME,
                        "Collection Manager CLI not found. Set its path "
                        "in the 'Lazer collections' section, or untick "
                        "'Merge generated .osdb files…'."
                    )
                    return
                cm_cli_cmd = cfg.command
                self.cm_cli_edit.setText(shlex.join(cfg.command))
            else:
                try:
                    cm_cli_cmd = shlex.split(raw)
                except ValueError:
                    cm_cli_cmd = raw.split()

        # Resolve the target collection choice into a single name override
        # (or None if the user wants the default per-collection naming).
        target_text = self.target_combo.currentText()
        target_name: str | None = None
        if add_to_lazer:
            if target_text == self.NEW_TARGET:
                new_name = self.new_name_edit.text().strip()
                if not new_name:
                    QMessageBox.warning(
                        self, APP_NAME,
                        "Pick a name for the new collection."
                    )
                    return
                target_name = new_name
            elif target_text and target_text != self.DEFAULT_TARGET:
                # Pulled from existing list — use userData when present
                # (the visible label has " (N maps)" appended).
                ud = self.target_combo.currentData()
                target_name = ud if ud else target_text

        job = DownloadJob(
            collection_ids=ids,
            output_dir=out_dir,
            download_beatmaps=self.download_beatmaps_cb.isChecked(),
            generate_osdb=self.generate_osdb_cb.isChecked(),
            auto_import=self.auto_import_cb.isChecked(),
            osu_binary=self.osu_path_edit.text().strip() or None,
            import_parallel=self.import_parallel_spin.value(),
            import_delay_ms=self.import_delay_spin.value(),
            add_to_lazer_collections=add_to_lazer,
            cm_cli_command=cm_cli_cmd,
            lazer_realm_path=self.realm_edit.text().strip() or None,
            target_collection_name=target_name,
            restart_lazer_after=self.restart_lazer_cb.isChecked(),
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
        self.worker.awaiting_import_confirmation.connect(
            self._on_awaiting_import_confirmation
        )

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

    def _on_awaiting_import_confirmation(self, n_imports: int) -> None:
        """Modal prompt: 'has osu!lazer finished importing the maps?'

        Until the user clicks OK, the worker is blocked at the start of
        _merge_into_lazer. Clicking Cancel aborts the whole batch.
        """
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle(APP_NAME)
        msg.setText("Has osu!lazer finished importing the downloaded maps?")
        msg.setInformativeText(
            f"{n_imports} map(s) were sent to osu!lazer for import.\n\n"
            "osu!lazer processes imports asynchronously — it may still be "
            "extracting and hashing beatmaps in the background.\n\n"
            "Open osu!lazer and check that the import notifications have "
            "all finished, then click 'Continue merge'.\n\n"
            "WARNING: clicking Continue while imports are still in flight "
            "will terminate osu!lazer mid-import and the unfinished maps "
            "will not end up in the merged collection."
        )
        cont = msg.addButton("Continue merge", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Cancel batch", QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(cont)
        msg.exec()

        if msg.clickedButton() is cont:
            if self.worker:
                self.worker.confirm_merge_continue()
        else:
            if self.worker:
                self.worker.cancel()
            self._append_log("[cancelled by user before merge]")

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
