# Skip Already-Imported + Parallel Downloads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users of huge osu!collector collections (e.g. 11k-map collection 17391) skip downloading beatmapsets they already have in osu!lazer, while still composing the full collection. Expose download parallelism in the GUI (1–32) with mirror round-robin to use it politely.

**Architecture:** A per-collection "probe" step runs `cm.exe create -b <bids> -o probe.osdb -l <realm_parent>` before downloads — Collection Manager CLI loads lazer's BeatmapInfo DB and enriches the IDs it knows about. Parse probe.osdb (existing `OsdbReader.read()` already returns hash-only entries with beatmap_id=0, so `bm.beatmap_id > 0` cleanly separates resolved from unresolved). Resolved beatmap_ids → their set_ids get added to a skip-set; their md5s feed into the collection's generated .osdb so lazer's collection references the hash lazer actually has. The download phase filters by skip-set and uses a user-configurable `ThreadPoolExecutor` pool size. `BeatmapMirror.download()` picks a per-set primary URL by `set_id % len(urls)` to spread load across catboy/nerinyan/osu.direct at high parallelism.

**Tech Stack:** Python 3.11+, PyQt6, requests, psutil, Collection Manager CLI (via wine flatpak on Linux), pytest (newly added).

**Spec:** `docs/superpowers/specs/2026-05-12-skip-imported-and-parallel-downloads-design.md`

---

## File Structure

- `osu_collector_gui.py` (existing, 102 KB single-file app) — all production changes land here
- `tests/` (new) — pytest unit tests for the pure-function pieces (writer/reader round-trip, mirror URL rotation, probe parsing)
- `tests/conftest.py` (new) — pytest config, makes the repo root importable
- `requirements-dev.txt` (new) — `pytest>=8.0`
- `CHANGELOG.md` (existing) — add v0.6.0 entry
- `docs/superpowers/plans/2026-05-12-skip-imported-and-parallel-downloads.md` — this file

The decision to keep production code in one file follows the existing project pattern. Tests go in a sibling `tests/` directory rather than colocated, matching standard Python convention.

---

## Task 0: Set up pytest infrastructure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `requirements-dev.txt`

- [ ] **Step 1: Create the tests directory and init**

Run: `mkdir -p /var/home/red/Projects/Osu-Collector-GUI/tests`

Create `tests/__init__.py` with empty content (just a single newline).

- [ ] **Step 2: Create conftest.py**

Create `tests/conftest.py`:

```python
"""Pytest config: make the repo root importable so tests can `import osu_collector_gui`."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

- [ ] **Step 3: Create requirements-dev.txt**

Create `requirements-dev.txt`:

```
pytest>=8.0
```

- [ ] **Step 4: Install dev deps + smoke-check pytest**

Run:
```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest --version
```

Expected: pytest reports its version (>= 8.0), no errors.

- [ ] **Step 5: Add a trivial sanity test**

Create `tests/test_smoke.py`:

```python
def test_imports():
    import osu_collector_gui  # noqa: F401
    assert osu_collector_gui.APP_VERSION
```

Run: `pytest tests/test_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/__init__.py tests/conftest.py tests/test_smoke.py requirements-dev.txt
git commit -m "Add pytest infrastructure for unit tests"
```

---

## Task 1: OsdbWriter accepts prefer_md5_map parameter

**Why:** Probe gives us lazer's md5 for maps lazer already has. The generated .osdb (which becomes the lazer collection) should reference *that* md5 so the collection entry isn't a "ghost". This task adds the substitution capability; the wiring comes later.

**Files:**
- Test: `tests/test_osdb_writer.py` (new)
- Modify: `osu_collector_gui.py:339-345` (`OsdbWriter.write`)
- Modify: `osu_collector_gui.py:347-415` (`OsdbWriter.write_many`)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_osdb_writer.py`:

```python
"""Verify OsdbWriter.write honors the prefer_md5_map override."""
from osu_collector_gui import OsdbWriter, OsdbReader, CollectionInfo, BeatmapInfo


def _one_collection_with(beatmaps):
    return CollectionInfo(
        id=1, name="test", uploader="me",
        beatmap_count=len(beatmaps), beatmaps=beatmaps,
    )


def test_write_without_prefer_md5_map_preserves_original_md5(tmp_path):
    info = _one_collection_with([
        BeatmapInfo(beatmap_id=42, set_id=100, md5="aaa",
                    artist="A", title="T", diff_name="D"),
    ])
    dest = tmp_path / "out.osdb"
    OsdbWriter.write(dest, info)

    [parsed] = OsdbReader.read(dest)
    [bm] = [b for b in parsed.beatmaps if b.beatmap_id == 42]
    assert bm.md5 == "aaa"


def test_write_with_prefer_md5_map_overrides_md5(tmp_path):
    info = _one_collection_with([
        BeatmapInfo(beatmap_id=42, set_id=100, md5="aaa",
                    artist="A", title="T", diff_name="D"),
    ])
    dest = tmp_path / "out.osdb"
    OsdbWriter.write(dest, info, prefer_md5_map={42: "bbb"})

    [parsed] = OsdbReader.read(dest)
    [bm] = [b for b in parsed.beatmaps if b.beatmap_id == 42]
    assert bm.md5 == "bbb"


def test_write_with_prefer_md5_map_falls_back_for_missing_bid(tmp_path):
    info = _one_collection_with([
        BeatmapInfo(beatmap_id=42, set_id=100, md5="aaa",
                    artist="A", title="T", diff_name="D"),
        BeatmapInfo(beatmap_id=99, set_id=200, md5="ccc",
                    artist="A", title="T", diff_name="D"),
    ])
    dest = tmp_path / "out.osdb"
    OsdbWriter.write(dest, info, prefer_md5_map={42: "bbb"})

    [parsed] = OsdbReader.read(dest)
    md5_by_id = {b.beatmap_id: b.md5 for b in parsed.beatmaps if b.beatmap_id}
    assert md5_by_id[42] == "bbb"
    assert md5_by_id[99] == "ccc"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_osdb_writer.py -v`
Expected: 3 errors — `TypeError: write() got an unexpected keyword argument 'prefer_md5_map'` on the two tests that use it. The first test should pass already (no kwarg used).

- [ ] **Step 3: Implement prefer_md5_map**

In `osu_collector_gui.py`, replace the `write` and `write_many` signatures and the body of `write_many` where it emits `bm.md5`:

```python
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
```

And change the per-beatmap md5 emission inside `write_many` (currently line 395):

```python
                md5 = bm.md5 or ""
                if prefer_md5_map and bm.beatmap_id in prefer_md5_map:
                    md5 = prefer_md5_map[bm.beatmap_id]
                cls._write_string(body, md5)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_osdb_writer.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_osdb_writer.py osu_collector_gui.py
git commit -m "OsdbWriter: accept prefer_md5_map to override per-beatmap md5"
```

---

## Task 2: BeatmapMirror round-robin URL selection

**Why:** At 32 parallel downloads, hammering one mirror is rude and likely to get throttled. Round-robin picks a different primary per set_id so load spreads across catboy/nerinyan/osu.direct without coordination.

**Files:**
- Test: `tests/test_beatmap_mirror.py` (new)
- Modify: `osu_collector_gui.py:212-256` (`BeatmapMirror` class)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_beatmap_mirror.py`:

```python
"""Verify BeatmapMirror round-robins URLs by set_id."""
from osu_collector_gui import BeatmapMirror


def test_round_robin_default_rotates_by_set_id():
    m = BeatmapMirror(primary="https://a", fallbacks=["https://b", "https://c"])
    assert m._urls_for_set(0) == ["https://a", "https://b", "https://c"]
    assert m._urls_for_set(1) == ["https://b", "https://c", "https://a"]
    assert m._urls_for_set(2) == ["https://c", "https://a", "https://b"]
    assert m._urls_for_set(3) == ["https://a", "https://b", "https://c"]


def test_round_robin_disabled_keeps_static_order():
    m = BeatmapMirror(
        primary="https://a",
        fallbacks=["https://b", "https://c"],
        round_robin=False,
    )
    for sid in (0, 1, 7, 100):
        assert m._urls_for_set(sid) == ["https://a", "https://b", "https://c"]


def test_round_robin_with_single_mirror_is_no_op():
    m = BeatmapMirror(primary="https://only", fallbacks=[])
    assert m._urls_for_set(0) == ["https://only"]
    assert m._urls_for_set(99) == ["https://only"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_beatmap_mirror.py -v`
Expected: AttributeError on `_urls_for_set` (method doesn't exist yet).

- [ ] **Step 3: Implement round-robin**

In `osu_collector_gui.py`, replace the `BeatmapMirror.__init__` and refactor `download` to use a helper. The full replacement for the class (osu_collector_gui.py:212-256) becomes:

```python
class BeatmapMirror:
    """Downloads a single .osz from a mirror with retries + fallbacks."""

    def __init__(self, primary: str = DEFAULT_MIRROR,
                 fallbacks: Iterable[str] = FALLBACK_MIRRORS,
                 round_robin: bool = True) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.urls = [primary, *fallbacks]
        self.round_robin = round_robin

    def _urls_for_set(self, set_id: int) -> list[str]:
        """Return urls rotated so each set_id picks a different primary."""
        if not self.round_robin or len(self.urls) <= 1:
            return list(self.urls)
        offset = set_id % len(self.urls)
        return self.urls[offset:] + self.urls[:offset]

    def download(self, beatmapset_id: int, dest_dir: Path) -> Path | None:
        """Download .osz to dest_dir; return final path or None on failure."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None

        for base_url in self._urls_for_set(beatmapset_id):
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_beatmap_mirror.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_beatmap_mirror.py osu_collector_gui.py
git commit -m "BeatmapMirror: round-robin URLs by set_id to spread mirror load"
```

---

## Task 3: ProbeResult + CmCliRunner.probe_imported_beatmaps

**Why:** This is the dedup engine — calls CM CLI with the collection's beatmap_ids and returns which ones lazer has.

**Files:**
- Test: `tests/test_probe.py` (new)
- Modify: `osu_collector_gui.py:605-610` (insert `ProbeResult` dataclass near `CmCliConfig`)
- Modify: `osu_collector_gui.py:611-684` (add `probe_imported_beatmaps` method to `CmCliRunner`)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_probe.py`:

```python
"""Verify CmCliRunner.probe_imported_beatmaps invokes CM CLI correctly and parses results."""
from pathlib import Path
from unittest.mock import patch, MagicMock

from osu_collector_gui import (
    CmCliConfig, CmCliRunner, ProbeResult,
    OsdbWriter, CollectionInfo, BeatmapInfo,
)


def _write_fake_probe_osdb(dest: Path, resolved_beatmaps: list[BeatmapInfo]) -> None:
    """Write a probe.osdb shaped like what `cm create -b` would produce."""
    info = CollectionInfo(
        id=0, name="probe", uploader="cm-cli",
        beatmap_count=len(resolved_beatmaps),
        beatmaps=resolved_beatmaps,
    )
    OsdbWriter.write(dest, info)


def test_probe_writes_bids_to_realm_parent_and_returns_resolved(tmp_path):
    realm = tmp_path / "client.realm"
    realm.write_bytes(b"fake")
    tmp_dir = realm.parent / ".oc-gui-tmp"

    captured_argv: list[list[str]] = []

    def fake_run(argv, **kwargs):
        captured_argv.append(argv)
        # Find the `-o` argument and write a fake probe.osdb there.
        out_idx = argv.index("-o") + 1
        out_path = Path(argv[out_idx])
        _write_fake_probe_osdb(out_path, [
            BeatmapInfo(beatmap_id=42, set_id=100, md5="lazer-md5-aaa",
                        artist="A", title="T", diff_name="D"),
        ])
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    cfg = CmCliConfig(command=["/fake/cm.exe"], osu_location=None)
    runner = CmCliRunner(cfg)

    with patch("osu_collector_gui.subprocess.run", side_effect=fake_run):
        result = runner.probe_imported_beatmaps(realm, [42, 99])

    # Returned ProbeResult contains lazer's md5 for the resolved bid.
    assert isinstance(result, ProbeResult)
    assert 42 in result.resolved
    assert result.resolved[42].md5 == "lazer-md5-aaa"
    assert 99 not in result.resolved

    # Bids file was written under the realm's parent tmp dir.
    [argv] = captured_argv
    bids_idx = argv.index("-b") + 1
    bids_path = Path(argv[bids_idx])
    assert bids_path.parent == tmp_dir
    assert "42" in bids_path.read_text()
    assert "99" in bids_path.read_text()

    # -l points at the realm parent (NOT the realm file itself).
    l_idx = argv.index("-l") + 1
    assert Path(argv[l_idx]) == realm.parent


def test_probe_returns_empty_result_when_cm_cli_fails(tmp_path):
    realm = tmp_path / "client.realm"
    realm.write_bytes(b"fake")

    def fake_run(argv, **kwargs):
        # Write an output file but exit non-zero — _run raises RuntimeError.
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "wine crash"
        return result

    cfg = CmCliConfig(command=["/fake/cm.exe"], osu_location=None)
    runner = CmCliRunner(cfg)

    with patch("osu_collector_gui.subprocess.run", side_effect=fake_run):
        result = runner.probe_imported_beatmaps(realm, [42])

    assert result.resolved == {}


def test_probe_handles_empty_beatmap_id_list(tmp_path):
    realm = tmp_path / "client.realm"
    realm.write_bytes(b"fake")

    cfg = CmCliConfig(command=["/fake/cm.exe"], osu_location=None)
    runner = CmCliRunner(cfg)

    with patch("osu_collector_gui.subprocess.run") as run_mock:
        result = runner.probe_imported_beatmaps(realm, [])

    assert result.resolved == {}
    # Should not have invoked CM CLI for an empty list.
    run_mock.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_probe.py -v`
Expected: ImportError on `ProbeResult`, or AttributeError on `probe_imported_beatmaps`.

- [ ] **Step 3: Add ProbeResult dataclass**

In `osu_collector_gui.py`, insert just before the `CmCliConfig` definition (around line 605):

```python
@dataclass
class ProbeResult:
    """Result of asking CM CLI which beatmap_ids lazer has imported.

    `resolved` maps beatmap_id → BeatmapInfo with lazer's current md5
    and metadata. Any beatmap_id NOT in `resolved` is implicitly "lazer
    doesn't have it" — we don't need an explicit unresolved set.
    """
    resolved: dict[int, BeatmapInfo] = field(default_factory=dict)
```

- [ ] **Step 4: Add probe_imported_beatmaps to CmCliRunner**

In `osu_collector_gui.py`, add this method to `CmCliRunner` (after `import_osdb_to_realm`, before `_run`):

```python
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

        try:
            bids_file.write_text("\n".join(str(b) for b in beatmap_ids))

            argv = [*self.cfg.command, "create",
                    "-b", str(bids_file),
                    "-o", str(probe_osdb),
                    "-l", str(realm_path.parent)]
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
            for p in (bids_file, probe_osdb):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_probe.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_probe.py osu_collector_gui.py
git commit -m "CmCliRunner: add probe_imported_beatmaps for dedup queries"
```

---

## Task 4: Add new fields to DownloadJob

**Why:** The worker and UI need a typed home for the two new settings.

**Files:**
- Modify: `osu_collector_gui.py:909-927` (`DownloadJob` dataclass)

- [ ] **Step 1: Add the two new fields**

In `osu_collector_gui.py`, update the `DownloadJob` dataclass. Find the existing definition (it has `cleanup_after_import: bool = False` as its last field around line 926) and add two more lines just below it:

```python
    # Dedup
    skip_already_imported: bool = False       # probe lazer + skip its sets
    # Tuning
    download_parallel: int = 4                # 1..32 — concurrent .osz fetches
```

- [ ] **Step 2: Verify import still works**

Run: `python -c "from osu_collector_gui import DownloadJob; print(DownloadJob.__dataclass_fields__.keys())"`
Expected: Output includes `skip_already_imported` and `download_parallel`.

- [ ] **Step 3: Commit**

```bash
git add osu_collector_gui.py
git commit -m "DownloadJob: add skip_already_imported + download_parallel fields"
```

---

## Task 5: Wire the probe into DownloadWorker.run

**Why:** The probe runs per collection, before downloading, and produces (a) a skip-set of beatmapset IDs and (b) a probe_md5_map keyed by beatmap_id for the .osdb writer.

**Files:**
- Modify: `osu_collector_gui.py:1030-1107` (`DownloadWorker.run` per-collection loop)

Read context: the existing loop fetches the collection (line 1041), writes the .osdb if `generate_osdb` is set (line 1060), then downloads (line 1070). The probe lands between fetch and .osdb generation.

- [ ] **Step 1: Modify the per-collection loop body**

In `osu_collector_gui.py`, replace the section from `for idx, cid in enumerate(...)` through the end of the download phase (osu_collector_gui.py:1034-1102). Find the current code and replace with:

```python
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
```

- [ ] **Step 2: Add the gating helper**

Still in `DownloadWorker`, add a small predicate method just below `__init__` (around line 979, near `cancel`):

```python
    def _probe_enabled_for_job(self) -> bool:
        """All gates that must be true for the probe step to run."""
        return bool(
            self.job.skip_already_imported
            and self.job.add_to_lazer_collections
            and self.job.cm_cli_command
            and self.job.lazer_realm_path
            and Path(self.job.lazer_realm_path).expanduser().exists()
        )
```

- [ ] **Step 3: Verify the file still imports**

Run: `python -c "from osu_collector_gui import DownloadWorker; print('ok')"`
Expected: prints `ok` with no error.

- [ ] **Step 4: Run all existing tests to ensure no regression**

Run: `pytest tests/ -v`
Expected: all tests still pass (the three from Task 1, three from Task 2, three from Task 3, smoke from Task 0).

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py
git commit -m "DownloadWorker: probe lazer before downloads, skip already-imported sets"
```

---

## Task 6: Add the Parallel Downloads spinbox

**Why:** Exposes the new `download_parallel` field. Single GUI element, doesn't depend on Task 7.

**Files:**
- Modify: `osu_collector_gui.py:1553-...` (`MainWindow._build_ui`)
- Modify: `osu_collector_gui.py:1830-...` (`_load_settings`)
- Modify: `osu_collector_gui.py:1836-...` (`_save_settings`)

Read context: find the existing "Parallel imports" spinbox in `_build_ui`. It's named `self.spin_import_parallel`. The new spinbox sits next to it.

- [ ] **Step 1: Locate the existing parallel-imports spinbox**

Run: `grep -n "spin_import_parallel\|Parallel imports\|import_parallel" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py`

You should see lines where the import-parallel widget is created, added to a layout, loaded from settings, and saved. The new download-parallel widget follows the same pattern.

- [ ] **Step 2: Add the widget in _build_ui**

In `_build_ui`, immediately after the line that creates `self.spin_import_parallel` (and before/after its `addRow` or layout add), add:

```python
        self.spin_download_parallel = QSpinBox()
        self.spin_download_parallel.setRange(1, 32)
        self.spin_download_parallel.setValue(4)
        self.spin_download_parallel.setToolTip(
            "How many .osz downloads to run in parallel per collection.\n"
            "Higher = faster on fast connections, but be polite to mirrors —\n"
            "above ~16 some mirrors may throttle. Round-robins across\n"
            "catboy.best, nerinyan.moe, and osu.direct to spread the load."
        )
```

Add it to the same form/grid as `spin_import_parallel`. If that line looks like:
```python
        form.addRow("Parallel imports:", self.spin_import_parallel)
```
then add immediately after:
```python
        form.addRow("Parallel downloads:", self.spin_download_parallel)
```

- [ ] **Step 3: Wire into _load_settings**

In `_load_settings` (near the line that does `self.spin_import_parallel.setValue(...)`), add:

```python
        self.spin_download_parallel.setValue(
            int(s.get("download_parallel", 4))
        )
```

- [ ] **Step 4: Wire into _save_settings**

In `_save_settings` (near the line that does `"import_parallel": self.spin_import_parallel.value()`), add the new key to the dict:

```python
            "download_parallel": self.spin_download_parallel.value(),
```

- [ ] **Step 5: Smoke-launch the GUI to verify the widget appears**

Run:
```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python osu_collector_gui.py
```
Expected: the GUI window opens; "Parallel downloads" spinbox is visible next to "Parallel imports"; default value is 4; range allows 1 to 32. Close the window when satisfied.

- [ ] **Step 6: Commit**

```bash
git add osu_collector_gui.py
git commit -m "GUI: expose Parallel downloads (1..32) spinbox + persist setting"
```

---

## Task 7: Add the Skip-Already-Imported checkbox

**Why:** Exposes the new `skip_already_imported` field. Lives in the "Lazer collections" section since it depends on CM CLI being configured there.

**Files:**
- Modify: `osu_collector_gui.py` (`_build_ui`, `_load_settings`, `_save_settings`)

Read context: find the existing "Add downloaded maps to osu!lazer collections" group/section. The new checkbox lives there, just below the main "enable" toggle.

- [ ] **Step 1: Locate the Lazer collections section**

Run: `grep -n "add_to_lazer\|Add downloaded maps\|Lazer collections" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py | head -20`

Find the existing `self.chk_add_to_lazer` (or similarly-named) checkbox that enables the merge feature. The new checkbox is added beneath it in the same layout.

- [ ] **Step 2: Add the checkbox in _build_ui**

Just after the line that creates and adds the existing "add to lazer collections" checkbox, add:

```python
        self.chk_skip_already_imported = QCheckBox(
            "Skip beatmapsets already imported in osu!lazer"
        )
        self.chk_skip_already_imported.setToolTip(
            "Before downloading, ask Collection Manager CLI which beatmaps\n"
            "your osu!lazer install already has. Skip the .osz download for\n"
            "those sets — but still add them to the lazer collection so\n"
            "huge collections (e.g. 17391) compose without redownloading\n"
            "thousands of maps you already have.\n"
            "\n"
            "Requires Collection Manager CLI (configured above) and\n"
            "'Add downloaded maps to osu!lazer collections' to be ON."
        )
```

Add it to the same parent layout, immediately below the "add to lazer collections" widget.

- [ ] **Step 3: Add the gating helper**

In `MainWindow`, add a method (near the other `_on_*` slots):

```python
    def _update_skip_imported_enabled(self) -> None:
        """Enable the skip-imported checkbox only when CM CLI + add-to-lazer
        are both configured. Otherwise grey it out with an explanatory tooltip."""
        cm_ok = bool(self._resolve_cm_cli())
        merge_on = self.chk_add_to_lazer.isChecked()
        self.chk_skip_already_imported.setEnabled(cm_ok and merge_on)
        if not cm_ok:
            self.chk_skip_already_imported.setToolTip(
                "Configure Collection Manager CLI above to enable this option."
            )
        elif not merge_on:
            self.chk_skip_already_imported.setToolTip(
                "Enable 'Add downloaded maps to osu!lazer collections' to use this."
            )
```

NOTE: Replace `self.chk_add_to_lazer` with the actual attribute name from your grep in Step 1.

- [ ] **Step 4: Hook the gate into existing signals**

Find where `self.chk_add_to_lazer.stateChanged` (or `.toggled`) is connected to existing slots. Add a call to `_update_skip_imported_enabled` there. Also connect it to whatever signal fires when `_resolve_cm_cli` could change (e.g. the CM CLI path line edit's `editingFinished`).

Concretely, near the connections in `_build_ui`, add:

```python
        self.chk_add_to_lazer.toggled.connect(self._update_skip_imported_enabled)
        # Run once at startup so the initial state is correct.
        self._update_skip_imported_enabled()
```

- [ ] **Step 5: Wire into _load_settings**

In `_load_settings`, add:

```python
        self.chk_skip_already_imported.setChecked(
            bool(s.get("skip_already_imported", False))
        )
```

- [ ] **Step 6: Wire into _save_settings**

In `_save_settings`, add to the dict:

```python
            "skip_already_imported": self.chk_skip_already_imported.isChecked(),
```

- [ ] **Step 7: Smoke-launch the GUI**

Run:
```
python osu_collector_gui.py
```
Expected:
- Checkbox is visible under "Lazer collections" section
- When CM CLI is configured and add-to-lazer is ON: checkbox is enabled
- When add-to-lazer is OFF: checkbox is greyed out with the explanatory tooltip
- When CM CLI path is empty: checkbox is greyed out with the configure-tooltip

- [ ] **Step 8: Commit**

```bash
git add osu_collector_gui.py
git commit -m "GUI: add 'Skip already-imported' checkbox + gating + persist"
```

---

## Task 8: Wire DownloadJob in _on_start

**Why:** The GUI widgets and the worker need to be connected — without this, ticking the box does nothing.

**Files:**
- Modify: `osu_collector_gui.py:2273-...` (`MainWindow._on_start`)

- [ ] **Step 1: Locate the DownloadJob construction**

Run: `grep -n "DownloadJob(" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py`

You should see one call site in `_on_start`. The new fields go into that constructor call.

- [ ] **Step 2: Pass the new fields**

In the `DownloadJob(...)` call in `_on_start`, add the two new fields (place them with the other matching ones — `cleanup_after_import` is near where they belong):

```python
            skip_already_imported=self.chk_skip_already_imported.isChecked(),
            download_parallel=self.spin_download_parallel.value(),
```

- [ ] **Step 3: Verify the file imports**

Run: `python -c "from osu_collector_gui import MainWindow; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Run all tests**

Run: `pytest tests/ -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py
git commit -m "Wire skip-imported + download-parallel from GUI into DownloadJob"
```

---

## Task 9: Bump version + CHANGELOG entry

**Files:**
- Modify: `osu_collector_gui.py:62` (APP_VERSION)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump APP_VERSION**

In `osu_collector_gui.py`, line 62:

```python
APP_VERSION = "0.6.0"
```

- [ ] **Step 2: Add CHANGELOG entry**

In `CHANGELOG.md`, insert a new section at the top (after the `# Changelog` header line, before the existing `## [0.5.0]` section):

```markdown
## [0.6.0] — 2026-05-12

The "stop redownloading maps I already have" release. Huge osu!collector collections (e.g. 17391 with ~11k maps) now skip downloading any beatmapset where lazer already has at least one diff imported — but still compose the full collection in lazer.

### Added

- **Skip beatmapsets already imported in osu!lazer.** New checkbox under "Lazer collections". Before downloads start, Collection Manager CLI probes lazer's BeatmapInfo DB (`cm.exe create -b <bids> -l <realm-parent>`) to learn which beatmap_ids it has. Sets with at least one imported diff are skipped; their md5s still land in the resulting lazer collection so it composes correctly. Bonus: the .osdb is written using lazer's current md5 for resolved maps so collection entries aren't "ghost" rows when the mapper has updated diffs.
- **Configurable parallel download count.** New "Parallel downloads" spinbox (1..32, default 4 to preserve previous behavior). Above ~8, requests round-robin across the three configured mirrors (catboy.best / nerinyan.moe / osu.direct) so a single mirror doesn't take all the load.
- **Mirror round-robin** in `BeatmapMirror.download()` — each `set_id` picks a different primary URL via `set_id % len(urls)`, with the other mirrors retained as fallbacks. Disabled by passing `round_robin=False` to the constructor.

### Other

- New `tests/` directory with pytest unit tests for the writer/reader prefer-md5 path, mirror URL rotation, and CM CLI probe (mocked subprocess). `requirements-dev.txt` pins pytest.
```

- [ ] **Step 3: Verify the title bar shows the new version**

Run: `python osu_collector_gui.py`
Expected: window title reads `osu-collector-gui v0.6.0 by Red`. Close the window.

- [ ] **Step 4: Run all tests**

Run: `pytest tests/ -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py CHANGELOG.md
git commit -m "v0.6.0: skip already-imported sets + configurable parallel downloads"
```

---

## Task 10: Manual smoke tests (user-driven)

**Why:** The probe → download → merge end-to-end path goes through wine + CM CLI + a real lazer realm. Pytest can't cover that; the user runs these scenarios manually.

**Note for the user:** These are gate checks before pushing to GitHub. Do them in order. If any fails, stop and report the failure mode rather than continuing.

- [ ] **Smoke 1: Empty / fresh-install behavior**

If you have access to a fresh lazer install (or backup your realm to test):
1. Launch `osu_collector_gui.py` with CM CLI configured, "Add downloaded maps to lazer collections" ON, "Skip beatmapsets already imported" ON
2. Run a small collection (~10 maps)
3. Expected log: `[probe] lazer has 0/10 maps; skipping 0/N sets`. All sets download normally. Collection appears in lazer with all maps.

- [ ] **Smoke 2: Re-run the same collection with everything already imported**

After Smoke 1 finishes and lazer has imported the maps:
1. Run the SAME collection again with both checkboxes on
2. Expected log: `[probe] lazer has N/N maps; skipping S/T sets` where S ≈ T. The download phase completes near-instantly (no .osz downloads). The lazer collection still contains all maps after the merge step.

- [ ] **Smoke 3: Mixed — some sets imported, some not**

1. Pick a collection where you have ~half the maps imported, half not
2. Run with both checkboxes on
3. Expected log: probe reports the split; download phase only fetches the missing sets; merge step ends with the lazer collection containing every map from the osu!collector collection.

- [ ] **Smoke 4: Parallel download stress test**

1. Set "Parallel downloads" to 16
2. Run a moderate collection (~100 sets, all uncached)
3. Watch the log — downloads should clearly proceed faster than at 4. No mirror should hit prolonged 4xx/5xx. If you see one mirror persistently failing, retry at 8 to confirm it's a load issue not a code issue.

- [ ] **Smoke 5: The real test — collection 17391**

1. Both checkboxes on, parallel downloads at whatever feels right (8–16)
2. Run collection 17391 (~11k maps)
3. Expected: probe takes ~10–30s under wine; reports skipping a substantial fraction; downloads only the unimported sets; final merge ends with the 17391 collection containing all ~11k maps in lazer.

- [ ] **Smoke 6: Probe failure path (fail-open)**

To verify graceful degradation: temporarily break CM CLI (e.g. set its path to `/bin/false` in the GUI, or to a nonexistent path). Run with both checkboxes on.
Expected log: `[probe] failed: ... — proceeding without dedup`. The download proceeds as if dedup were off (all sets downloaded). No crash, no realm modification before user confirmation.

- [ ] **All smoke tests pass — ready for review/push decision**

Once all of the above succeed, the implementation is ready. Per existing user direction, the branch stays local until manual verification is complete. Pushing to `origin/main` is a separate decision.

---

## Self-Review

**Spec coverage** (vs. `docs/superpowers/specs/2026-05-12-skip-imported-and-parallel-downloads-design.md`):

- ✅ ProbeResult dataclass → Task 3
- ✅ CmCliRunner.probe_imported_beatmaps → Task 3
- ✅ OsdbReader.read() reuse (no read_full needed; existing code already handles hash-only with bid=0) → noted in Task 3 implementation comment
- ✅ OsdbWriter prefer_md5_map → Task 1
- ✅ BeatmapMirror round-robin → Task 2
- ✅ DownloadJob new fields → Task 4
- ✅ DownloadWorker probe step + filter + parallel count → Task 5
- ✅ UI parallel downloads spinbox → Task 6
- ✅ UI skip-imported checkbox + gating → Task 7
- ✅ Settings persistence → Tasks 6 & 7
- ✅ _on_start wiring → Task 8
- ✅ Version bump + CHANGELOG → Task 9
- ✅ Manual smoke tests → Task 10
- ✅ Error handling table (fail-open probe, fail-closed merge unchanged) → implemented in Task 3 + Task 5
- ✅ Wine sandbox path convention → noted in Task 3 (`<realm_parent>/.oc-gui-tmp/`)
- ✅ Cleanup of probe tempfiles → `finally` block in `probe_imported_beatmaps` (Task 3)

**Placeholder scan:** none — every step has either exact code, exact commands, or a concrete manual check with expected output.

**Type consistency:**
- `ProbeResult.resolved: dict[int, BeatmapInfo]` referenced consistently across Tasks 3 and 5 ✅
- `probe_md5_map: dict[int, str]` (bid → md5) referenced consistently in Tasks 1 and 5 ✅
- `skipped_set_ids: set[int]` used consistently in Task 5 ✅
- `_probe_enabled_for_job` method name used consistently in Task 5 (definition + 2 call sites) ✅
- `_urls_for_set` method on `BeatmapMirror` defined + used in Task 2 ✅
