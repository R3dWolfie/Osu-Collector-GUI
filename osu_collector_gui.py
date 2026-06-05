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
from PyQt6.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QScrollArea,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "osu-collector-gui"
APP_VERSION = "0.9.1"
APP_AUTHOR = "Red"


def _default_lazer_realm_path() -> Path:
    """Best-effort default for the user's client.realm location."""
    home = Path.home()
    if sys.platform == "win32":
        # osu!lazer on Windows stores data in %APPDATA%\osu
        return home / "AppData/Roaming/osu/client.realm"
    if sys.platform == "darwin":
        return home / "Library/Application Support/osu/client.realm"
    # Linux + everything else
    return home / ".local/share/osu/client.realm"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+https://github.com/R3dWolfie/Osu-Collector-GUI)"

OSU_COLLECTOR_API = "https://osucollector.com/api"
# Mirror download endpoints as URL templates — "{id}" is the beatmapset id.
# Templates (not a fixed "/d/<id>") let mirrors use different path schemes,
# e.g. Nekoha's /api4/download/. All verified to stream raw .osz bytes.
DEFAULT_MIRROR = "https://catboy.best/d/{id}"
FALLBACK_MIRRORS = [
    "https://api.nerinyan.moe/d/{id}",
    "https://osu.direct/d/{id}",                  # NOT api.osu.direct (no DNS)
    "https://mirror.nekoha.moe/api4/download/{id}",
    "https://beatconnect.io/b/{id}",
]

# After a mirror's connect fails, blacklist it for this many seconds so
# other parallel download slots don't waste their connect-timeout on it.
MIRROR_DEAD_TTL_S = 60

# Network limits — be polite to the mirrors
DOWNLOAD_PARALLEL = 24  # default worker threads; the per-mirror adaptive
                        # caps below are the real governor on concurrency
DOWNLOAD_TIMEOUT_S = 120
DOWNLOAD_CONNECT_TIMEOUT_S = 10   # fail fast if a mirror is rate-limiting our IP
HTTP_RETRIES = 3
HTTP_BACKOFF_S = 2

# Adaptive per-mirror concurrency (AIMD — like TCP congestion control).
# Each mirror starts at *_START simultaneous downloads and probes upward to
# *_MAX while it stays healthy; a 429/403 halves it (down to *_MIN) and the
# mirror cools down briefly. This self-tunes to the fastest rate each mirror
# tolerates — "max speed without rate-limiting" — with no fixed guess.
PER_MIRROR_START = 2         # start gentle so the opening burst across all
                             # parallel slots doesn't trip a 429 immediately
PER_MIRROR_MIN = 1
PER_MIRROR_MAX = 8
PER_MIRROR_PROBE_EVERY = 6   # consecutive successes per +1 to the cap
RATE_LIMIT_COOLDOWN_S = 8.0  # default pause for a 429 with no Retry-After
RATE_LIMIT_COOLDOWN_MAX = 30.0   # never sideline a mirror longer than this,
                                 # so one big Retry-After can't stall a set
DOWNLOAD_OVERALL_DEADLINE_S = 90   # give up on one set after this long and
                                   # move on, rather than blocking a worker

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
# Theme — v0.7.0 Cherry red on dark base
# ---------------------------------------------------------------------------
#
# Applied once via QApplication.setStyleSheet in main(). Colors:
#   accent (Cherry):  #e3344f → #ffa15f gradient on progress + primary button
#   surface:          #1e1e26 (window body) / #16161c (title-bar strip)
#   fields:           #2a2a35 with #3a3a48 borders / #5a5a68 on focus
#   text:             #e8e8ec primary / #9aa0a6 muted / #7d8090 meta
#   semantic:         #5dd56e success (skipped) / #e3344f errors

# Sizing uses pt (not px) for fonts so text stays a consistent *physical*
# size across 1080p and 4K — the root cause of the old "doesn't scale"
# complaint was px fonts that rendered tiny on high-DPI displays. Paddings
# and radii stay in px; Qt scales those by the device pixel ratio under the
# PassThrough rounding policy set in main().
QSS = """
QWidget {
    background-color: #131419;
    color: #e9eaf0;
    font-family: "Segoe UI", "SF Pro Text", -apple-system, "Cantarell",
                 "Noto Sans", sans-serif;
    font-size: 10pt;
}

QLabel { background: transparent; color: #e9eaf0; }
QLabel#appTitle { font-size: 16pt; font-weight: 800; color: #ffffff; }
QLabel#appSubtitle { font-size: 9pt; color: #6f7388; }
QLabel[role="field"] {
    color: #8b8fa3;
    font-size: 8pt;
    font-weight: 700;
    letter-spacing: 0.7px;
    text-transform: uppercase;
}
QLabel[role="section"] {
    color: #e3344f;
    font-size: 8.5pt;
    font-weight: 800;
    letter-spacing: 0.8px;
    text-transform: uppercase;
}
QLabel[role="status"] { color: #8b8fa3; font-size: 9pt; }

/* Cards — the main grouping surface. Children paint transparent so the
   card colour shows through (the global QWidget rule would otherwise tile
   the window colour over every sub-widget). */
QFrame#card {
    background-color: #1b1c25;
    border: 1px solid #282a38;
    border-radius: 12px;
}
QFrame#card > QLabel,
QFrame#card > QWidget > QLabel { background: transparent; }

QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {
    background-color: #23252f;
    border: 1px solid #323545;
    border-radius: 8px;
    padding: 8px 11px;
    color: #e9eaf0;
    selection-background-color: #e3344f;
    selection-color: white;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #e3344f;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover { border-color: #3e4255; }
QLineEdit::placeholder, QPlainTextEdit::placeholder { color: #5a5e72; }

/* Combo popup themed; the down-arrow + spinbox arrows keep Qt's native
   rendering — every QSS override of those sub-controls renders blank or
   inconsistent across Linux/Windows/macOS. */
QComboBox QAbstractItemView {
    background-color: #23252f;
    border: 1px solid #323545;
    border-radius: 8px;
    color: #e9eaf0;
    padding: 4px;
    selection-background-color: #e3344f;
    selection-color: white;
    outline: none;
}

QPushButton, QToolButton {
    background-color: #262835;
    border: 1px solid #343748;
    border-radius: 8px;
    padding: 9px 16px;
    color: #e9eaf0;
    font-weight: 600;
}
QPushButton:hover, QToolButton:hover {
    background-color: #2e3140;
    border-color: #464a60;
}
QPushButton:pressed, QToolButton:pressed { background-color: #202230; }
QPushButton:disabled, QToolButton:disabled {
    color: #565a6e;
    background-color: #1c1d27;
    border-color: #282a38;
}

/* Primary call-to-action — the cherry-red brand gradient. */
QPushButton#primaryBtn {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #e3344f, stop:1 #ff6a47);
    border: none;
    color: white;
    font-size: 10.5pt;
    font-weight: 800;
    padding: 12px 22px;
    border-radius: 9px;
}
QPushButton#primaryBtn:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #ec4258, stop:1 #ff7a56);
}
QPushButton#primaryBtn:pressed {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #c92d44, stop:1 #e85b3e);
}
QPushButton#primaryBtn:disabled {
    background-color: #3a2730; color: #8a6f78;
}

/* Cancel / destructive secondary. */
QPushButton#dangerBtn {
    background-color: transparent;
    border: 1px solid #5e3640;
    color: #ff8ba0;
    font-weight: 700;
    padding: 12px 22px;
}
QPushButton#dangerBtn:hover { background-color: #2a1a20; border-color: #7a4450; }

/* Square-ish icon buttons (browse "…", refresh). */
QToolButton#iconBtn {
    padding: 8px 10px;
    min-width: 18px;
    font-weight: 700;
    color: #b8bccd;
}

QCheckBox {
    background: transparent;
    color: #c7cbd9;
    spacing: 9px;
    font-size: 9.5pt;
    padding: 3px 0;
}
/* QCheckBox::indicator kept native — QSS overrides drop the checkmark
   glyph on most platforms. */

QProgressBar {
    background-color: #23252f;
    border: none;
    border-radius: 5px;
    min-height: 9px;
    max-height: 9px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #e3344f, stop:1 #ffa15f);
    border-radius: 5px;
}

QPlainTextEdit#logBox {
    background-color: #0c0d12;
    border: 1px solid #23252f;
    border-radius: 9px;
    padding: 10px 12px;
    color: #98a0b8;
    font-family: "Cascadia Code", "SF Mono", "Consolas",
                 "DejaVu Sans Mono", monospace;
    font-size: 9pt;
}

QToolButton#advancedExpander {
    background: transparent;
    border: 1px solid #282a38;
    border-radius: 8px;
    color: #8b8fa3;
    font-size: 9.5pt;
    font-weight: 600;
    padding: 9px 12px;
    text-align: left;
}
QToolButton#advancedExpander:hover { color: #e9eaf0; border-color: #343748; }
QToolButton#advancedExpander:checked { color: #e9eaf0; }

QScrollBar:vertical { background: transparent; width: 9px; margin: 0; }
QScrollBar::handle:vertical {
    background: #343748; border-radius: 4px; min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #464a60; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""

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

def _parse_retry_after(value: str | None, cap: float = 600.0) -> float | None:
    """Parse an HTTP Retry-After header into a delay in seconds.

    Accepts either a delta-seconds integer ("120") or an HTTP-date. Returns
    None when absent/unparseable so the caller can fall back to its default.
    Capped so a misbehaving mirror can't blacklist itself for hours.
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), cap)
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return min(max(delta, 0.0), cap) if delta > 0 else None
    except (TypeError, ValueError):
        return None


class BeatmapMirror:
    """Downloads a single .osz from a mirror with retries + fallbacks.

    Shares a process-wide dead-mirror blacklist across instances so that
    when one parallel download slot detects a mirror is blocking/timing
    out at TCP-connect time, the other 9 slots skip that mirror for the
    next MIRROR_DEAD_TTL_S seconds and go straight to a working one.
    Without this, every slot independently rediscovers the same dead
    mirror, wasting ~10s connect-timeout each.
    """

    # Class-level shared state, all guarded by _state_lock:
    #   _dead_until: {url -> monotonic time the blacklist expires}
    #   _active:     {url -> current concurrent-download count}
    #   _limit:      {url -> current adaptive concurrency cap (AIMD)}
    #   _success:    {url -> consecutive successes since the last cap bump}
    #   _rr_index:   monotonically increasing round-robin cursor; each
    #                download starts at the next mirror so load spreads
    #                evenly (1->2->3->4->1...) even at low parallelism,
    #                instead of every download hammering the primary.
    # The lock is held ONLY during pick + increment/decrement. HTTP
    # I/O never runs under the lock.
    _dead_until: dict[str, float] = {}
    _active: dict[str, int] = {}
    _limit: dict[str, int] = {}
    _success: dict[str, int] = {}
    _rr_index: int = 0
    _state_lock = __import__("threading").Lock()

    def __init__(self, primary: str = DEFAULT_MIRROR,
                 fallbacks: Iterable[str] = FALLBACK_MIRRORS,
                 extra: Iterable[str] = (),
                 pool_maxsize: int = 32) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # Size the connection pool to the max parallelism so high worker
        # counts get real concurrency. The stock Session pools only 10
        # connections per host, so >10 parallel downloads to one mirror
        # would queue (or churn connections) — a hidden serialization.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=8, pool_maxsize=pool_maxsize,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        # User-supplied custom mirrors go first so they're preferred, then
        # the built-ins. De-duplicated, order preserved.
        seen: set[str] = set()
        self.urls = [
            u for u in [*extra, primary, *fallbacks]
            if u and not (u in seen or seen.add(u))
        ]

    @staticmethod
    def normalize_template(raw: str) -> str | None:
        """Turn a user-entered mirror URL into a download template.

        Accepts a full template containing "{id}", or a base URL to which
        "/{id}" is appended. Returns None for empty/non-http input.
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        if not (raw.startswith("http://") or raw.startswith("https://")):
            return None
        if "{id}" in raw:
            return raw
        return raw.rstrip("/") + "/{id}"

    @classmethod
    def _next_start(cls) -> int:
        """Return the next round-robin offset into the mirror list."""
        with cls._state_lock:
            i = cls._rr_index
            cls._rr_index += 1
            return i

    @classmethod
    def _is_dead(cls, url: str) -> bool:
        with cls._state_lock:
            until = cls._dead_until.get(url, 0.0)
            if until > time.monotonic():
                return True
            if until:
                cls._dead_until.pop(url, None)
            return False

    @classmethod
    def _mark_dead(cls, url: str, seconds: float = MIRROR_DEAD_TTL_S) -> None:
        """Blacklist a mirror for `seconds` (process-wide, so every parallel
        slot skips it). Never shortens an existing, longer blacklist."""
        with cls._state_lock:
            until = time.monotonic() + max(1.0, seconds)
            if until > cls._dead_until.get(url, 0.0):
                cls._dead_until[url] = until

    @classmethod
    def reset_state(cls) -> None:
        """Clear dead-cache + active counts + adaptive caps. Tests + reset."""
        with cls._state_lock:
            cls._dead_until.clear()
            cls._active.clear()
            cls._limit.clear()
            cls._success.clear()
            cls._rr_index = 0

    @classmethod
    def on_rate_limited(cls, url: str, retry_after: float | None) -> float:
        """A mirror returned 429/403: multiplicatively halve its concurrency
        cap (down to the floor) and cool it down briefly. Returns the
        cooldown applied (seconds)."""
        cooldown = retry_after if retry_after else RATE_LIMIT_COOLDOWN_S
        cooldown = min(cooldown, RATE_LIMIT_COOLDOWN_MAX)
        with cls._state_lock:
            cur = cls._limit.get(url, PER_MIRROR_START)
            cls._limit[url] = max(PER_MIRROR_MIN, cur // 2)
            cls._success[url] = 0
            until = time.monotonic() + max(1.0, cooldown)
            if until > cls._dead_until.get(url, 0.0):
                cls._dead_until[url] = until
        return cooldown

    @classmethod
    def on_success(cls, url: str) -> None:
        """A clean download: additively probe the cap upward (one step per
        PER_MIRROR_PROBE_EVERY consecutive successes), up to the ceiling."""
        with cls._state_lock:
            cur = cls._limit.get(url, PER_MIRROR_START)
            if cur >= PER_MIRROR_MAX:
                cls._success[url] = 0
                return
            n = cls._success.get(url, 0) + 1
            if n >= PER_MIRROR_PROBE_EVERY:
                cls._limit[url] = min(PER_MIRROR_MAX, cur + 1)
                cls._success[url] = 0
            else:
                cls._success[url] = n

    @classmethod
    def _acquire_least_busy(cls, candidates: list[str],
                           excluding: set[str],
                           respect_caps: bool = False) -> str | None:
        """Atomically pick the least-busy alive mirror among `candidates`
        not in `excluding`, and increment its active count.

        Tie-break by index in `candidates` so the declared primary
        (typically catboy) wins on cold start and on equal counts.

        With respect_caps=False (the default), falls back to allowing dead
        mirrors when every alive candidate is excluded — better to attempt
        and surface the failure than to refuse to try at all. Returns None
        only when every candidate is in `excluding`.

        With respect_caps=True (the real download path), a mirror is only
        eligible while its active count is below its adaptive cap, and dead
        mirrors are NOT used as a fallback — None then means "nothing free
        right now, wait and retry" (cooling down or at capacity), which the
        caller distinguishes from "exhausted" via its own exclude set.
        """
        with cls._state_lock:
            now = time.monotonic()
            def _alive(u: str) -> bool:
                until = cls._dead_until.get(u, 0.0)
                if until <= now:
                    if until:
                        cls._dead_until.pop(u, None)
                    return True
                return False

            available = [u for u in candidates
                         if u not in excluding and _alive(u)]
            if respect_caps:
                # Only mirrors under their current adaptive cap are eligible;
                # no dead-mirror fallback (we'd rather wait for a cooldown).
                available = [
                    u for u in available
                    if cls._active.get(u, 0) < cls._limit.get(u, PER_MIRROR_START)
                ]
            elif not available:
                # Every alive candidate excluded — allow dead mirrors so
                # the caller still gets to try.
                available = [u for u in candidates if u not in excluding]
            if not available:
                return None

            chosen = min(
                available,
                key=lambda u: (cls._active.get(u, 0), candidates.index(u)),
            )
            cls._active[chosen] = cls._active.get(chosen, 0) + 1
            return chosen

    @classmethod
    def _release(cls, url: str) -> None:
        """Decrement active count for `url`. Pop the entry if count
        reaches 0. Must pair with a successful _acquire_least_busy call."""
        with cls._state_lock:
            n = cls._active.get(url, 0)
            if n <= 1:
                cls._active.pop(url, None)
            else:
                cls._active[url] = n - 1

    def download(self, beatmapset_id: int, dest_dir: Path) -> Path | None:
        """Download .osz to dest_dir; return final path or None on failure.

        Mirror selection combines round-robin, adaptive per-mirror
        concurrency, and a shared dead-list:

        - Each download starts at the next mirror in rotation (1, 2, 3, 4,
          1, …) so no single mirror takes all the load.
        - A mirror is only used while its in-flight count is below its
          adaptive cap, which probes upward on success and halves on a 429
          — self-tuning to each mirror's tolerated speed.
        - A 429/403 cools the mirror down briefly (without giving up on the
          set); a 404 means this mirror doesn't have the set (try another);
          a connection/timeout failure blacklists the mirror process-wide.

        When every eligible mirror is momentarily at capacity or cooling
        down, the call waits and retries (up to an overall deadline) rather
        than failing — that's the back-pressure that keeps us at max speed
        without tripping rate limits.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        # Mirrors permanently out for THIS set: a 404 (not hosted here) or a
        # hard connection failure. Distinct from the transient at-cap /
        # cooling-down state, which we wait out instead of excluding.
        exhausted: set[str] = set()
        n_mirrors = len(self.urls)

        start = self._next_start() % n_mirrors
        ordered = self.urls[start:] + self.urls[:start]

        deadline = time.monotonic() + DOWNLOAD_OVERALL_DEADLINE_S

        while time.monotonic() < deadline:
            if len(exhausted) >= n_mirrors:
                break   # every mirror 404'd or hard-failed — genuinely gone

            base_url = self._acquire_least_busy(
                ordered, excluding=exhausted, respect_caps=True,
            )
            if base_url is None:
                # All non-exhausted mirrors are at capacity or cooling down.
                # Wait for a slot/cooldown rather than failing.
                time.sleep(0.2)
                continue

            try:
                url = base_url.format(id=beatmapset_id)
                with self.session.get(url, stream=True,
                                      timeout=(DOWNLOAD_CONNECT_TIMEOUT_S,
                                               DOWNLOAD_TIMEOUT_S),
                                      allow_redirects=True) as r:
                    if r.status_code == 404:
                        # Coverage differs per mirror — a 404 here does NOT
                        # mean the set is gone everywhere. Drop this mirror
                        # for this set and try the others.
                        exhausted.add(base_url)
                        continue
                    if r.status_code in (429, 403):
                        # Rate-limited: halve this mirror's cap and cool it
                        # down (honouring Retry-After). Keep the set in play
                        # — retry after the cooldown, possibly elsewhere.
                        cooldown = self.on_rate_limited(
                            base_url, _parse_retry_after(r.headers.get("Retry-After"))
                        )
                        last_error = requests.HTTPError(
                            f"{r.status_code} from {base_url}; cap→"
                            f"{self._limit.get(base_url)}, cooldown {int(cooldown)}s"
                        )
                        continue
                    r.raise_for_status()

                    filename = self._filename_from_response(r, beatmapset_id)
                    dest = dest_dir / filename
                    # Already have a complete copy on disk — skip the body
                    # download (saves bandwidth on re-runs and sidesteps the
                    # Windows rename-onto-existing-file failure below).
                    if dest.exists() and dest.stat().st_size > 0:
                        self.on_success(base_url)
                        return dest
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    head = b""
                    written = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=64 * 1024):
                            if chunk:
                                if not head:
                                    head = chunk[:4]
                                written += len(chunk)
                                f.write(chunk)

                    # Validate it's actually a .osz (ZIP) and complete. Some
                    # mirrors answer 200 with a Cloudflare/rate-limit HTML or
                    # JSON page instead of the file; saved as .osz that makes
                    # osu!lazer report "Beatmap import failed". A short read vs
                    # Content-Length means a truncated download. Reject either
                    # and try another mirror.
                    expected = r.headers.get("Content-Length")
                    truncated = (expected is not None
                                 and expected.isdigit()
                                 and written < int(expected))
                    if head[:2] != b"PK" or written < 1024 or truncated:
                        tmp.unlink(missing_ok=True)
                        # A mirror serving garbage for a set it claims (200)
                        # is misbehaving — cool it down so other slots skip
                        # it briefly too, and try the next mirror for this set.
                        self._mark_dead(base_url, HTTP_BACKOFF_S)
                        exhausted.add(base_url)
                        why = ("truncated" if truncated
                               else "non-.osz response")
                        last_error = requests.HTTPError(
                            f"{base_url} returned a {why} for {beatmapset_id}"
                        )
                        continue

                    # os.replace overwrites atomically on every platform;
                    # plain rename() raises FileExistsError (WinError 183)
                    # on Windows when dest already exists.
                    tmp.replace(dest)
                    self.on_success(base_url)
                    return dest
            except (requests.ConnectionError, requests.Timeout) as e:
                # TCP-level failure: blacklist this mirror process-wide and
                # drop it for this set so we don't wait on it.
                self._mark_dead(base_url)
                exhausted.add(base_url)
                last_error = e
                continue
            except requests.RequestException as e:
                # Other transient HTTP error (5xx, etc.): brief cooldown on
                # this mirror, then let the loop pick another.
                self._mark_dead(base_url, HTTP_BACKOFF_S)
                last_error = e
                continue
            finally:
                self._release(base_url)

        # Exhausted every mirror, or hit the deadline.
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
    def write(cls, dest_path: Path, info: CollectionInfo,
              prefer_md5_map: dict[int, str] | None = None) -> None:
        if not info.beatmaps:
            raise ValueError(
                "OsdbWriter requires per-beatmap details — call "
                "fetch_collection(..., with_beatmap_details=True) first."
            )
        cls.write_many(dest_path, [info], prefer_md5_map=prefer_md5_map)

    @classmethod
    def write_many(cls, dest_path: Path,
                   collections: list[CollectionInfo],
                   editor: str | None = None,
                   prefer_md5_map: dict[int, str] | None = None) -> None:
        """Write one .osdb file containing one or more collections.

        Writes o!dm8 format: an uncompressed "o!dm8" header followed by
        a gzip-wrapped body. The body re-states the version string and
        contains per-collection metadata + per-beatmap entries. CM CLI
        only writes o!dm8 these days, so the v8 reader path is the
        well-tested one — emitting v6 (uncompressed) hits an older code
        path in CM that throws EndOfStreamException on perfectly valid
        files.

        Per-collection layout (v8):
            string  name
            int32   OnlineId (-1 if none)
            int32   beatmap_count
            for each beatmap:
                int32  MapId
                int32  MapSetId
                string Artist
                string Title
                string DiffName
                string Md5
                string UserComment
                byte   PlayMode
                double StarRating
            int32   hash_only_count
            for each hash_only: string md5
        Then a single trailing footer string "By Piotrekol".
        """
        body = io.BytesIO()
        cls._write_string(body, "o!dm8")
        body.write(struct.pack("<d", cls._to_oadate(datetime.now(timezone.utc))))
        cls._write_string(body, editor or (collections[0].uploader if collections else "Unknown"))
        body.write(struct.pack("<i", len(collections)))

        for info in collections:
            cls._write_string(body, info.name or "Unknown")
            body.write(struct.pack("<i", info.id if info.id else -1))
            body.write(struct.pack("<i", len(info.beatmaps)))
            for bm in info.beatmaps:
                body.write(struct.pack("<i", bm.beatmap_id))
                body.write(struct.pack("<i", bm.set_id))
                cls._write_string(body, bm.artist or "Unknown")
                cls._write_string(body, bm.title or "Unknown")
                cls._write_string(body, bm.diff_name or "Unknown")
                md5 = bm.md5 or ""
                if prefer_md5_map and bm.beatmap_id in prefer_md5_map:
                    md5 = prefer_md5_map[bm.beatmap_id]
                cls._write_string(body, md5)
                cls._write_string(body, "")  # user comment
                body.write(bytes([max(0, min(3, bm.mode))]))
                body.write(struct.pack("<d", float(bm.star_rating)))
            body.write(struct.pack("<i", 0))   # no hash-only beatmaps

        cls._write_string(body, "By Piotrekol")

        # Wrap the body in a gzip stream. CM CLI uses SharpCompress's
        # GZipArchive which produces a standard gzip envelope; Python's
        # stdlib gzip is wire-compatible.
        import gzip as _gz
        compressed = _gz.compress(body.getvalue(), compresslevel=6)

        # Final file: uncompressed "o!dm8" header + gzip stream.
        out = io.BytesIO()
        cls._write_string(out, "o!dm8")
        out.write(compressed)

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(out.getvalue())


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
class ProbeResult:
    """Result of asking CM CLI which beatmap_ids lazer has imported.

    `resolved` maps beatmap_id → BeatmapInfo with lazer's current md5
    and metadata. Any beatmap_id NOT in `resolved` is implicitly "lazer
    doesn't have it" — we don't need an explicit unresolved set.
    """
    resolved: dict[int, BeatmapInfo] = field(default_factory=dict)


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

    def convert_osdb_to_db(self, src_osdb: Path, dest_db: Path) -> None:
        """Convert an .osdb to osu! stable's native collection.db format.

        Same `cm convert` command as the others — output format is inferred
        from the .db extension on -o. The .db output is accepted by
        osu!collector.com, osu! stable, and osu!lazer.
        """
        argv = [*self.cfg.command, "convert",
                "-i", str(src_osdb),
                "-o", str(dest_db),
                "-s"]
        self._run(argv)

    def probe_imported_beatmaps(self, realm_path: Path,
                                beatmap_ids: list[int]) -> ProbeResult:
        """Ask CM CLI which of `beatmap_ids` lazer's BeatmapInfo DB knows.

        Runs `cm.exe create -b <bids_file> -o probe.osdb -l <realm_parent>`.
        CM loads lazer's beatmap DB (because of -l) and enriches each id it
        recognizes with full metadata. Unrecognized ids end up as hash-only
        entries in the resulting .osdb (which we don't need to parse — they
        have beatmap_id=0 when OsdbReader returns them, easy to filter out).

        The bids file and probe.osdb live in the realm's parent .oc-gui-tmp/
        dir — same wine-sandbox-safe convention used by the merge step.

        Fail-open: any error returns an empty ProbeResult so the caller
        falls through to downloading everything (vs. fail-closed merge
        step, which refuses to write on read failure).
        """
        if not beatmap_ids:
            return ProbeResult()

        tmp_dir = realm_path.parent / ".oc-gui-tmp"
        tmp_dir.mkdir(exist_ok=True)
        bids_file = tmp_dir / "probe-bids.txt"
        probe_osdb = tmp_dir / "probe.osdb"

        # Snapshot the realm into a probe-only subdir so CM CLI doesn't
        # contend with a running osu!lazer for client.realm's lock. CM
        # CLI -l expects a directory containing a file literally named
        # "client.realm", so the snapshot must keep that filename. Realm
        # is MVCC — a file-level copy is a consistent point-in-time view.
        probe_realm_dir = tmp_dir / "probe-realm"
        probe_realm_dir.mkdir(exist_ok=True)
        probe_realm = probe_realm_dir / "client.realm"

        try:
            try:
                shutil.copy2(realm_path, probe_realm)
            except OSError:
                # Snapshot failed (disk full, perms, etc.) — fail open
                # so the caller proceeds to a full download.
                return ProbeResult()

            bids_file.write_text("\n".join(str(b) for b in beatmap_ids))

            argv = [*self.cfg.command, "create",
                    "-b", str(bids_file),
                    "-o", str(probe_osdb),
                    "-l", str(probe_realm_dir)]
            self._run(argv)

            if not probe_osdb.exists() or probe_osdb.stat().st_size == 0:
                return ProbeResult()

            parsed = OsdbReader.read(probe_osdb)
            if not parsed:
                return ProbeResult()

            # probe.osdb contains exactly one synthetic collection;
            # resolved entries have beatmap_id > 0, hash-only entries
            # have beatmap_id == 0 (and we don't care about them).
            resolved = {bm.beatmap_id: bm
                        for c in parsed
                        for bm in c.beatmaps
                        if bm.beatmap_id > 0}
            return ProbeResult(resolved=resolved)
        except Exception:
            return ProbeResult()
        finally:
            for p in (probe_osdb, probe_realm):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                probe_realm_dir.rmdir()
            except OSError:
                pass


    DEBUG_LOG = Path("/tmp/oc-cm-cli-debug.log") if sys.platform != "win32" \
        else Path(os.environ.get("TEMP", ".")) / "oc-cm-cli-debug.log"

    @classmethod
    def _run(cls, argv: list[str]) -> None:
        # Always dump the full invocation + output to a debug log so we
        # can actually see what CM CLI did, even when the GUI's error
        # dialog truncates a multi-thousand-line wine register dump.
        with cls.DEBUG_LOG.open("a", encoding="utf-8") as dlog:
            dlog.write("\n" + "=" * 70 + "\n")
            dlog.write(f"timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            dlog.write(f"argv: {shlex.join(argv)}\n")
            dlog.flush()

            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=600,
                # Use a proper /dev/null for stdin so .NET doesn't
                # block trying to read from a tty it doesn't have.
                stdin=subprocess.DEVNULL,
            )
            dlog.write(f"exit: {proc.returncode}\n")
            dlog.write(f"--- stdout ---\n{proc.stdout}\n")
            dlog.write(f"--- stderr ---\n{proc.stderr}\n")
            dlog.flush()

        if proc.returncode != 0:
            # Trim wine's gigantic register dump out of the user-facing
            # error — keep the human-readable preamble + a pointer to
            # the debug log.
            stderr = proc.stderr or ""
            for marker in ("Unhandled exception:", "Register dump:", "Backtrace:"):
                idx = stderr.find(marker)
                if idx > 0:
                    stderr = stderr[:idx].rstrip()
                    break
            raise RuntimeError(
                f"CM CLI failed (exit {proc.returncode}):\n"
                f"  cmd: {shlex.join(argv)}\n"
                f"  stdout: {proc.stdout.strip()}\n"
                f"  stderr: {stderr.strip()}\n"
                f"\nFull invocation logged to {cls.DEBUG_LOG}"
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
            base = home / "AppData/Local/osulazer"
            # Current osu!lazer ships with Velopack, which keeps the live
            # exe in a "current" subfolder (swapped atomically on update).
            # This is THE common case on any recent install — check first.
            p = base / "current" / "osu!.exe"
            if p.exists():
                candidates.append(p)
            # Legacy Squirrel.Windows installs dropped it under an
            # "app-X.Y.Z" subfolder instead.
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
    extra_mirrors: list[str] = field(default_factory=list)  # user templates
    # Lazer collection merging via CM CLI
    add_to_lazer_collections: bool = False
    cm_cli_command: list[str] | None = None  # full argv prefix
    lazer_realm_path: str | None = None      # path to client.realm
    target_collection_name: str | None = None  # if set, all maps go here
    restart_lazer_after: bool = False
    # Post-import housekeeping
    cleanup_after_import: bool = False       # delete <id> - <name>/ folders
    # Dedup
    skip_already_imported: bool = True        # probe lazer + skip its sets
    # Tuning
    download_parallel: int = DOWNLOAD_PARALLEL  # worker threads; per-mirror
                                                # adaptive caps govern the rest


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
        self.mirror = BeatmapMirror(primary=job.mirror_url,
                                    extra=job.extra_mirrors)
        # Always construct the importer so we know the lazer binary path
        # for the post-merge restart, even if auto_import is off. The
        # _maybe_import path checks job.auto_import before actually
        # invoking it, so this is purely about knowing the binary.
        self.importer = OsuLazerImporter(binary_override=job.osu_binary)
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
        # Set by _lazer_kill_if_running so the relaunch step can use the
        # exact same binary the user had running, even if our standard
        # search paths wouldn't find it.
        self._discovered_lazer_exe: Path | None = None
        # .osdb files we wrote during this run, so the merge step picks
        # up only the new ones — not stale .osdb files left over from
        # previous batches in the same output directory.
        self._generated_osdb_files: list[Path] = []
        if job.auto_import and self.importer.binary:
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

    def _probe_enabled_for_job(self) -> bool:
        """All gates that must be true for the probe step to run."""
        return bool(
            self.job.skip_already_imported
            and self.job.add_to_lazer_collections
            and self.job.cm_cli_command
            and self.job.lazer_realm_path
            and Path(self.job.lazer_realm_path).expanduser().exists()
        )

    def confirm_merge_continue(self) -> None:
        """Called from the GUI thread when the user clicks OK on the
        'did osu!lazer finish importing?' dialog. Releases the worker
        from its wait at the start of _merge_into_lazer."""
        self._continue_merge_event.set()

    # ---- helpers ----------------------------------------------------------

    def _do_import(self, path: Path) -> None:
        """Run on an import-pool worker; handles delay throttling."""
        if not self.job.auto_import or not self.importer.binary:
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

        # Make a missing lazer binary LOUD. Without this the auto-import
        # path no-ops silently — maps download fine but never reach the
        # game, with no clue why.
        if self.job.auto_import and not self.importer.binary:
            self.log.emit(
                "[lazer] WARNING: auto-import is ON but no osu!lazer "
                "executable was found. Downloaded maps will NOT be imported "
                "into the game. Set the 'osu!lazer binary' path in the "
                "Advanced section (e.g. Windows: "
                r"%LOCALAPPDATA%\osulazer\current\osu!.exe)."
            )

        for idx, cid in enumerate(self.job.collection_ids, 1):
            if self._cancelled:
                self.log.emit("[cancelled]")
                break

            # The probe and .osdb generation both need per-beatmap details
            # (beatmap_id + md5). Force the detail fetch when EITHER feature
            # is on, even if the user didn't tick "generate .osdb".
            need_details = (
                self.job.generate_osdb
                or self._probe_enabled_for_job()
            )
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

            # --- probe lazer for which sets it already has ---
            skipped_set_ids: set[int] = set()
            probe_md5_map: dict[int, str] = {}
            if self._probe_enabled_for_job() and info.beatmaps and not self._cancelled:
                try:
                    self.log.emit(
                        f"  [probe] querying lazer for {len(info.beatmaps)} beatmap IDs..."
                    )
                    cm = CmCliRunner(CmCliConfig(
                        command=list(self.job.cm_cli_command),
                        osu_location=None,
                    ))
                    realm = Path(self.job.lazer_realm_path).expanduser()
                    probe = cm.probe_imported_beatmaps(
                        realm, [b.beatmap_id for b in info.beatmaps if b.beatmap_id]
                    )
                    probe_md5_map = {bid: bm.md5 for bid, bm in probe.resolved.items() if bm.md5}
                    skipped_set_ids = {
                        b.set_id for b in info.beatmaps
                        if b.beatmap_id in probe.resolved and b.set_id
                    }
                    self.log.emit(
                        f"  [probe] lazer has {len(probe.resolved)}/{len(info.beatmaps)} maps; "
                        f"skipping {len(skipped_set_ids)}/{len(info.beatmapset_ids)} sets"
                    )
                except Exception as e:
                    # Fail-open: probe failures cost bandwidth, not data.
                    self.log.emit(f"  [probe] failed: {e} — proceeding without dedup")

            ok = 0
            failed = 0
            skipped = 0

            # --- generate .osdb (independent of beatmap downloads) ---
            if self.job.generate_osdb:
                try:
                    osdb_path = col_dir / f"{safe_name}.osdb"
                    OsdbWriter.write(osdb_path, info,
                                     prefer_md5_map=probe_md5_map or None)
                    self._generated_osdb_files.append(osdb_path)
                    self.log.emit(f"  [.osdb] {osdb_path.name}")
                except Exception as e:
                    self.log.emit(f"  [.osdb error] {e}")

            # --- download beatmaps in parallel ---
            if self.job.download_beatmaps and info.beatmapset_ids:
                set_ids = info.beatmapset_ids
                workers = max(1, min(32, self.job.download_parallel))
                done = 0
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = {}
                    for sid in set_ids:
                        if sid in skipped_set_ids:
                            continue
                        futures[ex.submit(self._download_one, sid, col_dir)] = sid
                    # Tick the bar for every set already skipped so progress
                    # accurately reflects total work.
                    if skipped_set_ids:
                        done = len(skipped_set_ids & set(set_ids))
                        skipped = done
                        self.beatmap_progress.emit(done, len(set_ids))
                        self.log.emit(
                            f"  [skip] {skipped} set(s) already imported in lazer"
                        )
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
            self.log.emit(
                f"=== {info.name}: {ok} ok, {failed} failed, "
                f"{skipped} skipped (already imported) ==="
            )
            if ok > 0 or skipped > 0 or self.job.generate_osdb:
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
        elif self.job.auto_import and self._import_calls_issued > 0 and not self._cancelled:
            # No merge step to gate cleanup behind, but we still need
            # the user to confirm imports finished before deleting the
            # source files lazer might still be reading.
            self._continue_merge_event.clear()
            self.awaiting_import_confirmation.emit(self._import_calls_issued)
            self._continue_merge_event.wait(timeout=3600)

        # --- cleanup per-collection folders ---
        if self.job.cleanup_after_import and not self._cancelled:
            try:
                self._cleanup_collection_folders()
            except Exception as e:
                self.log.emit(f"[cleanup] failed: {e}")

        self.batch_finished.emit(ok_collections, total)

    # ---- post-import cleanup ---------------------------------------------

    def _cleanup_collection_folders(self) -> None:
        """Delete the per-collection download folders, keeping everything
        else (especially anything that looks like a Realm file).

        Pattern matched: top-level dirs in output_dir whose name starts
        with '<digits> - '. That's the convention osu-collector-dl uses
        and what our worker creates. We refuse to delete:
            - the /db subdirectory
            - any file with .realm in its name (incl. backups)
            - anything not a directory
            - paths outside output_dir
        """
        out_dir = Path(self.job.output_dir).resolve()
        if not out_dir.exists() or not out_dir.is_dir():
            return

        deleted = 0
        kept = 0
        for entry in out_dir.iterdir():
            try:
                # Hard safety: only descend into things directly inside
                # out_dir, never follow symlinks pointing elsewhere.
                if entry.is_symlink():
                    kept += 1
                    continue
                if not entry.is_dir():
                    kept += 1
                    continue
                if entry.name == "db":
                    kept += 1
                    continue
                if entry.name.startswith("."):
                    # Hidden temp dirs we manage ourselves (.cm_tmp, etc.)
                    kept += 1
                    continue
                # Match the "<id> - <name>" pattern.
                if not re.match(r"^\d+\s*[-–]\s*", entry.name):
                    kept += 1
                    continue
                # Belt-and-braces: refuse if anything inside has .realm
                # in its name.
                contains_realm = False
                for sub in entry.rglob("*.realm*"):
                    contains_realm = True
                    break
                if contains_realm:
                    self.log.emit(
                        f"[cleanup] SKIP {entry.name}: contains a .realm file"
                    )
                    kept += 1
                    continue

                shutil.rmtree(entry)
                deleted += 1
            except OSError as e:
                self.log.emit(f"[cleanup] couldn't remove {entry.name}: {e}")
                kept += 1

        self.log.emit(
            f"[cleanup] removed {deleted} collection folder(s), kept {kept} other entr(ies)"
        )

    # ---- lazer collection merge ------------------------------------------

    def _merge_into_lazer(self) -> None:
        # Short-circuit if nothing was generated this run (e.g. all
        # collections failed to fetch, or every set was skipped). Without
        # this guard, the expensive snapshot+export through wine runs even
        # when there's nothing to merge — looks like the GUI hangs.
        if not self._generated_osdb_files:
            self.log.emit("[lazer] no new collections generated this run — skipping merge")
            return

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

        # Collect ONLY .osdb files we generated this run. Previously we
        # rglob'd output_dir which picked up stale .osdb files from
        # earlier batches and silently merged them, producing way more
        # collections than the user actually downloaded.
        new_collections: list[CollectionInfo] = []
        for f in self._generated_osdb_files:
            if not f.exists():
                self.log.emit(f"[lazer] WARN: generated {f.name} is missing")
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
        for p in psutil.process_iter(attrs=["name", "exe", "cmdline"]):
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

        # Snapshot the exe path of the process we're about to kill so we
        # can relaunch the SAME binary later — no guessing needed.
        # AppImages are special: psutil.exe returns /tmp/.mount_<hash>/...
        # which is unmounted the moment the AppImage exits. We need the
        # original .AppImage path. Order of preference:
        #   1. $APPIMAGE env var (set by every AppImage runtime)
        #   2. cmdline[0] if it's a .AppImage file
        #   3. /proc/PID/exe (works for non-AppImage installs)
        for p in targets:
            try:
                discovered: Path | None = None

                # 1. APPIMAGE env var — the canonical path
                try:
                    env = p.environ()
                    appimage = env.get("APPIMAGE")
                    if appimage and Path(appimage).exists():
                        discovered = Path(appimage)
                except (psutil.NoSuchProcess, psutil.AccessDenied,
                        FileNotFoundError, OSError):
                    pass

                # 2. cmdline[0] — what the user actually invoked
                if discovered is None:
                    cmdline = p.info.get("cmdline") or []
                    if cmdline:
                        c0 = Path(cmdline[0]).expanduser()
                        if c0.exists() and (
                            c0.suffix.lower() == ".appimage"
                            or "osu" in c0.name.lower()
                        ):
                            discovered = c0

                # 3. exe symlink — only if it points somewhere persistent
                if discovered is None:
                    exe = p.info.get("exe")
                    if exe and Path(exe).exists() and "/.mount_" not in exe:
                        discovered = Path(exe)

                if discovered is not None:
                    self._discovered_lazer_exe = discovered
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

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
        # Prefer the exe path we snapshotted from the running process —
        # that's guaranteed to be the user's actual binary, no
        # heuristics needed.
        binary = self._discovered_lazer_exe or self.importer.binary
        if not binary:
            self.log.emit(
                "[lazer] no binary path discovered or configured — "
                "skipping relaunch. Set 'osu!lazer binary' in the Tuning "
                "section if you want auto-restart to work."
            )
            return
        self.log.emit(f"[lazer] relaunching {binary}")
        # Save the actual binary into the importer for any subsequent
        # operations in this run.
        self.importer.binary = binary
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
# UI helpers (pure functions, unit-testable without Qt)
# ---------------------------------------------------------------------------

def should_enable_start(collection_ids_text: str) -> bool:
    """The Start button is enabled iff at least one non-whitespace character
    appears in the collection-IDs field. The actual ID parsing happens at
    submit time via _parse_ids."""
    return bool(collection_ids_text.strip())


def target_combo_default_label() -> str:
    """The default sentinel item in the 'Add to' picker. Picking this
    preserves v0.6.x's behavior: one lazer collection per osu!collector
    collection, named after the collection."""
    return "(one collection per osu!collector collection)"


def target_combo_no_merge_label() -> str:
    """The sentinel item that disables lazer collection merge entirely.
    Files still download (and may still auto-import into lazer) but no
    realm modification happens."""
    return "Don't merge"


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} by {APP_AUTHOR}")
        # Roomy default; the layout is fully resizable — the log card soaks
        # up any extra vertical space so there's never dead air or squish.
        self.resize(780, 760)
        self.setMinimumSize(560, 600)

        self.thread: QThread | None = None
        self.worker: DownloadWorker | None = None
        self.settings = self._load_settings()

        self._build_ui()

    # ----- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        # Single resizable page. The download inputs sit in a card up top;
        # the log card at the bottom carries the vertical stretch so the
        # window resizes cleanly with no dead space and no squish. Advanced
        # is an inline collapsible card — opening it borrows space from the
        # log rather than forcing the window to grow.

        # Everything lives inside a scroll area: when the window is tall
        # enough the log card stretches to fill it; when it's too short
        # (e.g. Advanced open on a small window) the page scrolls instead of
        # squishing widgets on top of each other.
        scroll = QScrollArea()
        scroll.setObjectName("rootScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setCentralWidget(scroll)

        central = QWidget()
        scroll.setWidget(central)
        self._scroll_content = central
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # --- Header ---
        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("osu!collector")
        title.setObjectName("appTitle")
        subtitle = QLabel(f"downloader · v{APP_VERSION}")
        subtitle.setObjectName("appSubtitle")
        header.addWidget(title)
        header.addWidget(subtitle, alignment=Qt.AlignmentFlag.AlignBottom)
        header.addStretch(1)
        root.addLayout(header)

        # --- Download card ---
        card = self._make_card()
        cv = card.layout()

        cv.addWidget(self._field_label("Collection IDs or URLs"))
        self.ids_edit = QPlainTextEdit()
        self.ids_edit.setPlaceholderText(
            "Paste osu!collector IDs or links — one per line, "
            "or comma / space separated"
        )
        self.ids_edit.setMinimumHeight(56)
        self.ids_edit.setMaximumHeight(84)
        self.ids_edit.textChanged.connect(self._update_start_enabled)
        cv.addWidget(self.ids_edit)

        # Output folder
        cv.addWidget(self._field_label("Output folder"))
        self.dir_edit = QLineEdit(self.settings.get(
            "last_output_dir", str(Path.home() / "osu-collections")
        ))
        self.dir_browse_btn = self._icon_button("…", self._on_browse,
                                                "Choose output folder")
        cv.addLayout(self._input_row(self.dir_edit, self.dir_browse_btn))

        # Add-to picker + refresh
        cv.addWidget(self._field_label("Add maps to osu!lazer collection"))
        self.target_combo = QComboBox()
        self.target_combo.setEditable(False)
        # Don't auto-grow to the longest collection name — clamp the width
        # so a long name can't blow out the layout.
        self.target_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.target_combo.setMinimumContentsLength(18)
        self._reset_target_combo()
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        self.refresh_collections_btn = self._icon_button(
            "⟳", self._on_refresh_collections,
            "Load your existing osu!lazer collections from client.realm "
            "(via Collection Manager CLI)."
        )
        cv.addLayout(self._input_row(self.target_combo,
                                     self.refresh_collections_btn))

        # New-collection-name row (only shown for "Create new…")
        self.new_name_label = self._field_label("New collection name")
        self.new_name_label.setVisible(False)
        cv.addWidget(self.new_name_label)
        self.new_name_edit = QLineEdit()
        self.new_name_edit.setPlaceholderText("Name of the new collection")
        self.new_name_edit.setText(self.settings.get("new_collection_name", ""))
        self.new_name_edit.setVisible(False)
        cv.addWidget(self.new_name_edit)

        # Parallelism spinboxes
        spin_row = QHBoxLayout()
        spin_row.setSpacing(14)
        self.download_parallel_spin = QSpinBox()
        self.download_parallel_spin.setRange(1, 32)
        self.download_parallel_spin.setValue(int(self.settings.get("download_parallel", DOWNLOAD_PARALLEL)))
        self.download_parallel_spin.setToolTip(
            "Upper bound on simultaneous downloads. Each mirror is throttled "
            "adaptively underneath this, so it's safe to leave high — the app "
            "speeds up until a mirror pushes back, then backs off that mirror."
        )
        spin_row.addLayout(self._labeled("Parallel downloads",
                                         self.download_parallel_spin), stretch=1)
        self.import_parallel_spin = QSpinBox()
        self.import_parallel_spin.setRange(1, 8)
        self.import_parallel_spin.setValue(int(self.settings.get("import_parallel", 1)))
        self.import_parallel_spin.setToolTip("How many imports to hand osu!lazer at once.")
        spin_row.addLayout(self._labeled("Parallel imports",
                                         self.import_parallel_spin), stretch=1)
        cv.addLayout(spin_row)

        root.addWidget(card)

        # --- Action row: Start/Cancel (swap) + Export ---
        action_row = QHBoxLayout()
        action_row.setSpacing(10)

        self.start_btn = QPushButton("Start download")
        self.start_btn.setObjectName("primaryBtn")
        self.start_btn.clicked.connect(self._on_start)
        self.start_btn.setEnabled(False)
        action_row.addWidget(self.start_btn, stretch=2)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setVisible(False)
        action_row.addWidget(self.cancel_btn, stretch=2)

        self.export_btn = QPushButton("Export to .db…")
        self.export_btn.setToolTip(
            "Export the collection currently selected above to osu! stable's\n"
            "native collection.db format (accepted by osu!collector.com,\n"
            "stable, and lazer). Requires CM CLI + a real lazer collection\n"
            "picked (not a 'Don't merge' or default sentinel)."
        )
        self.export_btn.clicked.connect(self._on_export_collection)
        action_row.addWidget(self.export_btn, stretch=1)
        root.addLayout(action_row)

        # --- Status + progress ---
        self.status_label = QLabel("Ready")
        self.status_label.setProperty("role", "status")
        root.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        # --- Advanced (collapsible card) ---
        self.advanced_expander = QToolButton()
        self.advanced_expander.setObjectName("advancedExpander")
        self.advanced_expander.setCheckable(True)
        self.advanced_expander.setText("▸  Advanced settings")
        self.advanced_expander.setSizePolicy(QSizePolicy.Policy.Expanding,
                                             QSizePolicy.Policy.Fixed)
        self.advanced_expander.toggled.connect(self._on_advanced_toggled)
        root.addWidget(self.advanced_expander)

        self.advanced_container = self._make_card()
        self._build_advanced(self.advanced_container)
        self.advanced_container.setVisible(False)
        root.addWidget(self.advanced_container)

        # --- Log card (carries the vertical stretch) ---
        log_card = self._make_card()
        lv = log_card.layout()
        lv.addWidget(self._field_label("Activity log"))
        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(96)
        self.log_box.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)
        self.log_box.setPlainText(
            "Ready. Paste a collection ID or URL above and click "
            "Start download to begin."
        )
        lv.addWidget(self.log_box, stretch=1)
        root.addWidget(log_card, stretch=1)

        # Restore advanced-expanded state from settings.
        if self.settings.get("advanced_expanded", False):
            self.advanced_expander.setChecked(True)

        # Sync the new-collection-name field to the restored picker value
        # (setCurrentIndex during _reset_target_combo runs with signals
        # blocked, so the handler wouldn't otherwise fire on startup).
        self._on_target_changed(self.target_combo.currentIndex())
        self._update_start_enabled()
        QTimer.singleShot(0, self._sync_scroll_min)

    def _sync_scroll_min(self) -> None:
        """Pin the scroll content's minimum height to its natural height so
        the scroll area scrolls (rather than letting QVBoxLayout crush
        widgets below their preferred size) when the window is too short.
        Above that height the log card's stretch fills the extra space."""
        c = self._scroll_content
        c.setMinimumHeight(c.sizeHint().height())

    # ----- UI building blocks ---------------------------------------------

    @staticmethod
    def _make_card() -> QFrame:
        """A rounded surface with an internal vertical layout."""
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 16)
        lay.setSpacing(8)
        return card

    @staticmethod
    def _field_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("role", "field")
        return lbl

    @staticmethod
    def _input_row(field: QWidget, button: QWidget) -> QHBoxLayout:
        """A line-edit/combo with a trailing icon button, snug together."""
        row = QHBoxLayout()
        row.setSpacing(7)
        row.addWidget(field, stretch=1)
        row.addWidget(button)
        return row

    def _labeled(self, text: str, widget: QWidget) -> QVBoxLayout:
        """A micro field-label stacked above a widget."""
        col = QVBoxLayout()
        col.setSpacing(5)
        col.addWidget(self._field_label(text))
        col.addWidget(widget)
        return col

    @staticmethod
    def _icon_button(glyph: str, handler, tooltip: str = "") -> QToolButton:
        btn = QToolButton()
        btn.setObjectName("iconBtn")
        btn.setText(glyph)
        if tooltip:
            btn.setToolTip(tooltip)
        btn.clicked.connect(handler)
        return btn

    def _build_advanced(self, parent: QFrame) -> None:
        """Build the contents of the collapsible Advanced card."""
        layout = parent.layout()  # the card's QVBoxLayout
        layout.setSpacing(7)

        def section(text: str, top: bool = False) -> None:
            lbl = QLabel(text)
            lbl.setProperty("role", "section")
            if not top:
                lbl.setContentsMargins(0, 8, 0, 0)
            layout.addWidget(lbl)

        # ---- Paths ----
        section("Paths", top=True)

        layout.addWidget(self._field_label("Collection Manager CLI command"))
        _saved_cmd = self.settings.get("cm_cli_command", [])
        _saved_cmd_text = shlex.join(_saved_cmd) if isinstance(_saved_cmd, list) and _saved_cmd else ""
        self.cm_cli_edit = QLineEdit(_saved_cmd_text)
        self.cm_cli_edit.setPlaceholderText("(auto-detect: native CM CLI or wine flatpak)")
        self.cm_cli_edit.textChanged.connect(self._update_skip_imported_enabled)
        cm_detect = QPushButton("Auto-detect")
        cm_detect.clicked.connect(self._on_detect_cm)
        layout.addLayout(self._input_row(self.cm_cli_edit, cm_detect))

        layout.addWidget(self._field_label("client.realm path"))
        self.realm_edit = QLineEdit(self.settings.get(
            "lazer_realm_path", str(_default_lazer_realm_path())
        ))
        layout.addLayout(self._input_row(
            self.realm_edit,
            self._icon_button("…", self._on_browse_realm, "Locate client.realm"),
        ))

        layout.addWidget(self._field_label("osu!lazer binary"))
        self.osu_path_edit = QLineEdit(self.settings.get("osu_binary", ""))
        self.osu_path_edit.setPlaceholderText("(auto-detect)")
        layout.addLayout(self._input_row(
            self.osu_path_edit,
            self._icon_button("…", self._on_browse_osu, "Locate the osu!lazer executable"),
        ))

        # ---- Mirrors ----
        section("Mirrors")
        layout.addWidget(self._field_label("Extra mirror URLs (one per line)"))
        self.custom_mirror_edit = QPlainTextEdit(self.settings.get("custom_mirrors", ""))
        self.custom_mirror_edit.setPlaceholderText(
            "https://my-mirror.example/d/{id}   —   {id} is the beatmapset id; "
            "a plain base URL gets /{id} appended. Tried before the built-ins."
        )
        self.custom_mirror_edit.setMaximumHeight(64)
        layout.addWidget(self.custom_mirror_edit)
        builtins = ", ".join(u.split("//", 1)[1].split("/", 1)[0] for u in
                             [DEFAULT_MIRROR, *FALLBACK_MIRRORS])
        hint = QLabel(f"Built-in mirrors: {builtins}")
        hint.setProperty("role", "status")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ---- Behaviour ----
        section("Behaviour")

        self.auto_import_cb = QCheckBox("Auto-import maps into osu!lazer as they download")
        self.auto_import_cb.setChecked(bool(self.settings.get("auto_import", True)))
        layout.addWidget(self.auto_import_cb)

        self.skip_imported_cb = QCheckBox("Skip beatmapsets osu!lazer already has")
        self.skip_imported_cb.setChecked(bool(self.settings.get("skip_already_imported", True)))
        layout.addWidget(self.skip_imported_cb)

        self.restart_lazer_cb = QCheckBox("Restart osu!lazer after merging collections")
        self.restart_lazer_cb.setChecked(bool(self.settings.get("restart_lazer_after", True)))
        layout.addWidget(self.restart_lazer_cb)

        self.generate_osdb_cb = QCheckBox("Generate .osdb files (export-only)")
        self.generate_osdb_cb.setChecked(bool(self.settings.get("generate_osdb", False)))
        layout.addWidget(self.generate_osdb_cb)

        self.consolidate_cb = QCheckBox("Consolidate .osdb files into a db/ subfolder")
        self.consolidate_cb.setChecked(bool(self.settings.get("consolidate_osdb", False)))
        layout.addWidget(self.consolidate_cb)

        self.cleanup_cb = QCheckBox("Delete download folders after import")
        self.cleanup_cb.setChecked(bool(self.settings.get("cleanup_after_import", False)))
        layout.addWidget(self.cleanup_cb)

        # ---- Tuning + maintenance ----
        section("Tuning")

        tune_row = QHBoxLayout()
        tune_row.setSpacing(14)
        self.import_delay_spin = QSpinBox()
        self.import_delay_spin.setRange(0, 5000)
        self.import_delay_spin.setSuffix(" ms")
        self.import_delay_spin.setSingleStep(50)
        self.import_delay_spin.setValue(int(self.settings.get("import_delay_ms", 300)))
        tune_row.addLayout(self._labeled("Import delay", self.import_delay_spin), stretch=1)

        self.recover_realm_btn = QPushButton("Recover realm from backup…")
        self.recover_realm_btn.clicked.connect(self._on_recover_realm)
        recover_col = QVBoxLayout()
        recover_col.setSpacing(5)
        recover_col.addWidget(self._field_label("Maintenance"))
        recover_col.addWidget(self.recover_realm_btn)
        tune_row.addLayout(recover_col, stretch=1)
        layout.addLayout(tune_row)

        # Keep the skip-imported gating logic alive even though the checkbox
        # is buried in Advanced (still needs CM CLI to be configured).
        self._update_skip_imported_enabled()

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
            "generate_osdb": self.generate_osdb_cb.isChecked(),
            "auto_import": self.auto_import_cb.isChecked(),
            "consolidate_osdb": self.consolidate_cb.isChecked(),
            "cleanup_after_import": self.cleanup_cb.isChecked(),
            "import_parallel": self.import_parallel_spin.value(),
            "download_parallel": self.download_parallel_spin.value(),
            "import_delay_ms": self.import_delay_spin.value(),
            "osu_binary": self.osu_path_edit.text(),
            "custom_mirrors": self.custom_mirror_edit.toPlainText(),
            "skip_already_imported": self.skip_imported_cb.isChecked(),
            "cm_cli_command": cm_list,
            "lazer_realm_path": self.realm_edit.text(),
            "target_collection": self.target_combo.currentText(),
            "new_collection_name": self.new_name_edit.text(),
            "restart_lazer_after": self.restart_lazer_cb.isChecked(),
            "advanced_expanded": self.advanced_expander.isChecked(),
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

    def _update_start_enabled(self) -> None:
        self.start_btn.setEnabled(should_enable_start(self.ids_edit.toPlainText()))

    def _on_advanced_toggled(self, checked: bool) -> None:
        self.advanced_container.setVisible(checked)
        self.advanced_expander.setText(
            "▾  Advanced settings" if checked else "▸  Advanced settings"
        )
        # Recompute the scroll content's minimum height now that the
        # Advanced card has shown/hidden, so the page scrolls if needed
        # instead of squishing. Deferred a tick so the layout has settled.
        QTimer.singleShot(0, self._sync_scroll_min)

    def _on_export_collection(self) -> None:
        """Export the picker-selected lazer collection to a single .db file."""
        # currentData() holds the bare collection name set as userData when
        # _on_refresh_collections populated the combo. currentText() would
        # include the " (N maps)" display suffix and never match the realm.
        target_name = self.target_combo.currentData()
        if not target_name:
            self.status_label.setText(
                "Export: pick a real collection from 'Add to' first "
                "(click Refresh to load your lazer collections)."
            )
            return
        target_name = str(target_name).strip()

        cm_cli_cmd = self._resolve_cm_cli()
        if not cm_cli_cmd:
            self.status_label.setText(
                "Export: Collection Manager CLI not configured "
                "(set its path in Advanced → Paths)."
            )
            return

        realm_path = Path(self.realm_edit.text().strip()).expanduser()
        if not realm_path.exists():
            self.status_label.setText(
                f"Export: client.realm not found at {realm_path}"
            )
            return

        # Suggest <CollectionName>.db in Downloads as the default save path.
        default_dir = Path.home() / "Downloads"
        default_name = _safe_filename(target_name) + ".db"
        dest_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export collection to .db",
            str(default_dir / default_name),
            "osu! collection database (*.db)",
        )
        if not dest_str:
            return
        dest_path = Path(dest_str)
        if dest_path.suffix.lower() != ".db":
            dest_path = dest_path.with_suffix(".db")

        self.status_label.setText(f"Exporting '{target_name}' to .db…")
        self.export_btn.setEnabled(False)
        QApplication.processEvents()

        try:
            self._do_export_collection(target_name, realm_path, dest_path, cm_cli_cmd)
            self.status_label.setText(f"Exported '{target_name}' to {dest_path}")
        except Exception as e:
            self.status_label.setText(f"Export failed: {e}")
        finally:
            self.export_btn.setEnabled(True)

    def _do_export_collection(self, collection_name: str, realm_path: Path,
                              dest_path: Path, cm_cli_cmd: list[str]) -> None:
        """Workhorse for _on_export_collection. Raises on failure."""
        cm = CmCliRunner(CmCliConfig(command=list(cm_cli_cmd), osu_location=None))
        tmp_dir = realm_path.parent / ".oc-gui-tmp"
        tmp_dir.mkdir(exist_ok=True)
        snapshot_realm = tmp_dir / "export-snapshot.realm"
        all_osdb = tmp_dir / "export-all.osdb"
        single_osdb = tmp_dir / "export-single.osdb"

        try:
            # Snapshot so lazer can stay open during export.
            shutil.copy2(realm_path, snapshot_realm)
            cm.export_realm_to_osdb(snapshot_realm, all_osdb)

            collections = OsdbReader.read(all_osdb)
            match = next(
                (c for c in collections if c.name.strip() == collection_name.strip()),
                None,
            )
            if match is None:
                raise RuntimeError(
                    f"Collection '{collection_name}' not found in your lazer "
                    f"realm. Click Refresh in 'Add to' to update the list."
                )

            # Write a single-collection .osdb, then convert that to .db.
            OsdbWriter.write(single_osdb, match)
            cm.convert_osdb_to_db(single_osdb, dest_path)
        finally:
            for p in (snapshot_realm, all_osdb, single_osdb):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

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

    DEFAULT_TARGET = target_combo_default_label()
    NEW_TARGET = "+ Create new collection..."
    SEPARATOR = "──────────"

    def _reset_target_combo(self) -> None:
        """Populate the target combo with just the default + 'Create new' + 'Don't merge'."""
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItem(self.DEFAULT_TARGET)
        self.target_combo.addItem(self.NEW_TARGET)
        self.target_combo.addItem(target_combo_no_merge_label())
        # Restore last-used selection if it still makes sense.
        saved = self.settings.get("target_collection", "")
        idx = self.target_combo.findText(saved) if saved else 0
        self.target_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.target_combo.blockSignals(False)

    def _on_target_changed(self, _idx: int) -> None:
        text = self.target_combo.currentText()
        show_new = (text == self.NEW_TARGET)
        self.new_name_edit.setVisible(show_new)
        self.new_name_label.setVisible(show_new)
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

    def _update_skip_imported_enabled(self) -> None:
        """Enable the skip-imported checkbox only when CM CLI is configured."""
        cm_ok = bool(self._resolve_cm_cli())
        self.skip_imported_cb.setEnabled(cm_ok)
        if not cm_ok:
            self.skip_imported_cb.setToolTip(
                "Configure Collection Manager CLI in Advanced to enable this option."
            )

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
        self.target_combo.addItem(target_combo_no_merge_label())
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

        # Resolve CM CLI for collection-merging features (skip-imported etc.).
        # cm_cli_cmd is None when the user hasn't configured CM CLI — that's fine;
        # add_to_lazer_collections will be False in that case.
        cm_cli_cmd: list[str] | None = self._resolve_cm_cli()

        target_text = self.target_combo.currentText()
        # add_to_lazer follows the picker: anything except the "Don't merge"
        # sentinel means we want the realm round-trip. Empty string can occur
        # mid-startup before the combo populates — treat that as merge-on too
        # so we don't accidentally disable merging due to a race condition.
        add_to_lazer = target_text != target_combo_no_merge_label()

        # Resolve the target collection choice into a single name override
        # (or None if the user wants the default per-collection naming).
        target_name: str | None = None
        if target_text == self.NEW_TARGET:
            new_name = self.new_name_edit.text().strip()
            if not new_name:
                QMessageBox.warning(
                    self, APP_NAME,
                    "Pick a name for the new collection."
                )
                return
            target_name = new_name
        elif target_text and target_text not in (self.DEFAULT_TARGET, target_combo_no_merge_label()):
            # Pulled from existing list — use userData when present
            # (the visible label has " (N maps)" appended).
            ud = self.target_combo.currentData()
            target_name = ud if ud else target_text

        # Parse any user-supplied custom mirrors (one per line) into templates.
        extra_mirrors: list[str] = []
        for line in self.custom_mirror_edit.toPlainText().splitlines():
            tmpl = BeatmapMirror.normalize_template(line)
            if tmpl:
                extra_mirrors.append(tmpl)

        job = DownloadJob(
            collection_ids=ids,
            output_dir=out_dir,
            download_beatmaps=True,
            extra_mirrors=extra_mirrors,
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
            cleanup_after_import=self.cleanup_cb.isChecked(),
            skip_already_imported=self.skip_imported_cb.isChecked(),
            download_parallel=self.download_parallel_spin.value(),
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

        self.log_box.clear()
        self.start_btn.setVisible(False)
        self.cancel_btn.setVisible(True)
        self.status_label.setText("Starting…")
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

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
        self.log_box.appendPlainText(msg)

    def _on_collection_started(self, idx: int, total: int, name: str, n_sets: int) -> None:
        self.status_label.setText(f"Collection {idx}/{total} — {name}  ({n_sets} sets)")
        if total > 0:
            self.progress_bar.setVisible(True)
            self.progress_bar.setMaximum(total * max(n_sets, 1))
            self.progress_bar.setValue((idx - 1) * max(n_sets, 1))

    def _on_beatmap_progress(self, current: int, total: int) -> None:
        self.status_label.setText(f"Beatmap {current} / {total}")
        self.progress_bar.setMaximum(max(total, 1))
        self.progress_bar.setValue(current)

    def _on_collection_finished(self, idx: int, ok: int, total: int) -> None:
        pass  # progress_bar updated via _on_beatmap_progress; no separate col bar

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
        self.status_label.setText(f"Done.  {ok}/{total} collections succeeded.")
        self.progress_bar.setVisible(False)
        if self.consolidate_cb.isChecked():
            self._consolidate_osdb()
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread = None
        self.worker = None
        self.start_btn.setVisible(True)
        self._update_start_enabled()
        self.cancel_btn.setVisible(False)

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
    # Honor non-integer Windows DPI scaling (125%, 150%) precisely instead
    # of rounding to the nearest integer factor — Qt 6's default Round
    # policy is the root cause of 1-pixel-off widget heights that fool the
    # QScrollArea's sizeHint() into underestimating content height on
    # Windows. Must be set BEFORE constructing QApplication.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    # A single point-sized base font so every widget scales from one source
    # and stays a consistent physical size from 1080p through 4K. Pixel
    # sizes in the QSS that need to differ are set per-role there.
    base_font = QFont(app.font())
    base_font.setPointSizeF(10.0)
    app.setFont(base_font)
    app.setStyleSheet(QSS)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
