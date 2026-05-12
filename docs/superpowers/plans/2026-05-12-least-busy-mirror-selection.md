# Least-Busy Mirror Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the static "primary first, fallback on error" mirror ordering in `BeatmapMirror` with dynamic least-busy selection backed by a process-wide active-connection counter. Catboy stays preferred while it's responding fast (its active count drops to 0 immediately); load shifts away when it stalls or its connections pile up.

**Architecture:** A single shared `_state_lock` guards both the existing dead-mirror blacklist and a new per-mirror active-count dict (`_active: dict[str, int]`). New `_acquire_least_busy(candidates, excluding) -> str | None` atomically picks the alive mirror with min active count (tie-break by mirror declaration order, so catboy wins ties), increments its count, and returns the url. `_release(url)` decrements. The HTTP call runs **outside the lock**, with `_release` in a `finally`. `download()` becomes a while-loop that acquires least-busy, tries the request with retries, falls to the next mirror on failure. The `round_robin` kwarg and `_urls_for_set` method are removed entirely — least-busy subsumes them.

**Tech Stack:** Python 3.11+, requests, threading, pytest.

**Spec:** `docs/superpowers/specs/2026-05-12-least-busy-mirror-selection-design.md`

---

## File Structure

All changes land in the existing single file `osu_collector_gui.py`. Test file updates land in `tests/test_beatmap_mirror.py` and `tests/conftest.py`. No new files.

| File | Change scope |
|---|---|
| `osu_collector_gui.py` | `BeatmapMirror` class: lock consolidation, new state, new methods, rewritten `download()`, removals |
| `tests/test_beatmap_mirror.py` | Delete 2 obsolete tests, retarget 2 dead-mirror tests, add 9 new least-busy tests |
| `tests/conftest.py` | Add autouse fixture to reset `BeatmapMirror` class state between tests |
| `CHANGELOG.md` | New v0.6.2 "Fixed" entry |

---

## Task 1: Lock consolidation + state structure + reset fixture

**Why:** The spec calls for a single `_state_lock` guarding both `_dead_until` and a new `_active` dict. Doing the consolidation first means subsequent tasks can rely on a stable state model. The autouse fixture in conftest ensures class-level state doesn't bleed between tests.

**Files:**
- Modify: `osu_collector_gui.py:218-261` (`BeatmapMirror` class state + dead-mirror methods)
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add autouse reset fixture to conftest.py**

Edit `/var/home/red/Projects/Osu-Collector-GUI/tests/conftest.py`. The current file makes the repo importable; append the fixture:

```python
"""Pytest config: make the repo root importable so tests can `import osu_collector_gui`."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_beatmap_mirror_state():
    """Clear BeatmapMirror's process-wide dead-cache and active-count
    state between tests so class-level state doesn't bleed."""
    from osu_collector_gui import BeatmapMirror
    BeatmapMirror.reset_state()
    yield
    BeatmapMirror.reset_state()
```

NOTE: this fixture calls `reset_state`, which doesn't exist yet — pytest will fail to collect tests until Step 2 lands. That's expected for the TDD red phase.

- [ ] **Step 2: Update BeatmapMirror class state + lock + reset method**

In `/var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py`, find the `BeatmapMirror` class (around line 218). Replace the class-state block at lines 229–232:

```python
    # Class-level cache: {url -> monotonic_until}. Each parallel download
    # slot sees the same blacklist within the lifetime of the process.
    _dead_until: dict[str, float] = {}
    _dead_lock = __import__("threading").Lock()
```

With:

```python
    # Class-level shared state, all guarded by _state_lock:
    #   _dead_until: {url -> monotonic time the blacklist expires}
    #   _active:     {url -> current concurrent-download count}
    # The lock is held ONLY during pick + increment/decrement. HTTP
    # I/O never runs under the lock.
    _dead_until: dict[str, float] = {}
    _active: dict[str, int] = {}
    _state_lock = __import__("threading").Lock()
```

Then update `_is_dead`, `_mark_dead`, and `reset_dead_mirrors` (currently lines 242–261). Replace those three methods with:

```python
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
    def _mark_dead(cls, url: str) -> None:
        with cls._state_lock:
            cls._dead_until[url] = time.monotonic() + MIRROR_DEAD_TTL_S

    @classmethod
    def reset_state(cls) -> None:
        """Clear dead-cache + active counts. For tests + manual reset."""
        with cls._state_lock:
            cls._dead_until.clear()
            cls._active.clear()
```

- [ ] **Step 3: Update existing tests that reference `reset_dead_mirrors`**

In `/var/home/red/Projects/Osu-Collector-GUI/tests/test_beatmap_mirror.py`, find every occurrence of `BeatmapMirror.reset_dead_mirrors()` and replace with `BeatmapMirror.reset_state()`. There are 4 occurrences in the existing two dead-mirror tests:

Lines:
```python
def test_dead_mirror_is_filtered_out_of_urls_for_set():
    BeatmapMirror.reset_dead_mirrors()
    ...
    BeatmapMirror.reset_dead_mirrors()


def test_all_dead_mirrors_still_returns_full_list_as_fallback():
    ...
    BeatmapMirror.reset_dead_mirrors()
    ...
    BeatmapMirror.reset_dead_mirrors()
```

Replace all four `reset_dead_mirrors()` calls with `reset_state()`. (These tests will be deleted/retargeted later in Task 5, but for now they need to compile.)

- [ ] **Step 4: Run tests, confirm green**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
pytest tests/ -v
```
Expected: all 12 tests pass. (The autouse fixture now finds `reset_state`; the existing tests still pass because the renamed method has the same effect plus also clears `_active`, which is currently always empty.)

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py tests/conftest.py tests/test_beatmap_mirror.py
git commit -m "BeatmapMirror: consolidate locks, add _active state, rename reset_dead_mirrors"
```

---

## Task 2: `_acquire_least_busy` with TDD

**Why:** The pick-and-increment primitive. Six unit tests cover cold-start, tie-break, least-busy selection, exclusion, dead-fallback, and the all-excluded sentinel.

**Files:**
- Modify: `tests/test_beatmap_mirror.py` (append new tests)
- Modify: `osu_collector_gui.py` (`BeatmapMirror` class — add method)

- [ ] **Step 1: Write the failing tests**

Append to `/var/home/red/Projects/Osu-Collector-GUI/tests/test_beatmap_mirror.py`:

```python
def test_acquire_picks_primary_on_cold_state():
    chosen = BeatmapMirror._acquire_least_busy(
        ["https://a", "https://b", "https://c"], excluding=set()
    )
    assert chosen == "https://a"
    assert BeatmapMirror._active == {"https://a": 1}


def test_acquire_breaks_ties_by_url_order():
    BeatmapMirror._active["https://a"] = 1
    BeatmapMirror._active["https://b"] = 1
    BeatmapMirror._active["https://c"] = 1
    chosen = BeatmapMirror._acquire_least_busy(
        ["https://a", "https://b", "https://c"], excluding=set()
    )
    assert chosen == "https://a"
    assert BeatmapMirror._active["https://a"] == 2


def test_acquire_picks_least_busy_when_not_tied():
    BeatmapMirror._active["https://a"] = 3
    BeatmapMirror._active["https://b"] = 1
    BeatmapMirror._active["https://c"] = 2
    chosen = BeatmapMirror._acquire_least_busy(
        ["https://a", "https://b", "https://c"], excluding=set()
    )
    assert chosen == "https://b"
    assert BeatmapMirror._active["https://b"] == 2


def test_acquire_excludes_listed_urls():
    chosen = BeatmapMirror._acquire_least_busy(
        ["https://a", "https://b", "https://c"], excluding={"https://a"}
    )
    assert chosen == "https://b"


def test_acquire_falls_back_to_dead_when_alive_set_is_empty():
    # Only candidate "a" is in our list; mark it dead. Excluding nothing.
    # Alive filter would return empty, so we fall back to allowing dead.
    BeatmapMirror._mark_dead("https://a")
    chosen = BeatmapMirror._acquire_least_busy(
        ["https://a"], excluding=set()
    )
    assert chosen == "https://a"


def test_acquire_returns_none_when_everything_excluded():
    chosen = BeatmapMirror._acquire_least_busy(
        ["https://a", "https://b", "https://c"],
        excluding={"https://a", "https://b", "https://c"},
    )
    assert chosen is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_beatmap_mirror.py -v
```
Expected: 6 failures — `AttributeError: type object 'BeatmapMirror' has no attribute '_acquire_least_busy'`.

- [ ] **Step 3: Implement `_acquire_least_busy`**

In `/var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py`, in the `BeatmapMirror` class, add this method directly after `reset_state`:

```python
    @classmethod
    def _acquire_least_busy(cls, candidates: list[str],
                           excluding: set[str]) -> str | None:
        """Atomically pick the least-busy alive mirror among `candidates`
        not in `excluding`, and increment its active count.

        Tie-break by index in `candidates` so the declared primary
        (typically catboy) wins on cold start and on equal counts.

        Falls back to allowing dead mirrors when every alive candidate
        is excluded — better to attempt and surface the failure than to
        refuse to try at all.

        Returns None only when every candidate is in `excluding`.
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
            if not available:
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
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_beatmap_mirror.py -v
```
Expected: 6 new tests pass + the existing 5 still pass = 11 total.

- [ ] **Step 5: Run full suite to confirm no regression**

```
pytest tests/ -v
```
Expected: all green (smoke + osdb + probe + mirror = 17 tests now).

- [ ] **Step 6: Commit**

```bash
git add osu_collector_gui.py tests/test_beatmap_mirror.py
git commit -m "BeatmapMirror: add _acquire_least_busy with tie-break + dead-fallback"
```

---

## Task 3: `_release` with TDD + concurrency stress test

**Why:** Decrements the active count. Must pair with every successful `_acquire_least_busy`. The stress test verifies that 50 threads acquiring/releasing in tight loops don't corrupt the counter.

**Files:**
- Modify: `tests/test_beatmap_mirror.py` (append)
- Modify: `osu_collector_gui.py` (`BeatmapMirror` — add method)

- [ ] **Step 1: Write the failing tests**

Append to `/var/home/red/Projects/Osu-Collector-GUI/tests/test_beatmap_mirror.py`:

```python
def test_release_decrements_and_pops_at_zero():
    BeatmapMirror._acquire_least_busy(["https://a"], excluding=set())
    BeatmapMirror._acquire_least_busy(["https://a"], excluding=set())
    assert BeatmapMirror._active["https://a"] == 2

    BeatmapMirror._release("https://a")
    assert BeatmapMirror._active["https://a"] == 1

    BeatmapMirror._release("https://a")
    assert "https://a" not in BeatmapMirror._active


def test_release_preserves_other_mirrors():
    BeatmapMirror._acquire_least_busy(["https://a", "https://b"], excluding=set())
    BeatmapMirror._acquire_least_busy(["https://a", "https://b"], excluding=set())
    # After two acquires from cold state with tie-break: a then b.
    assert BeatmapMirror._active == {"https://a": 1, "https://b": 1}

    BeatmapMirror._release("https://a")
    assert BeatmapMirror._active == {"https://b": 1}


def test_concurrent_acquire_release_does_not_corrupt_state():
    import threading

    urls = ["https://a", "https://b", "https://c", "https://d"]
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(100):
                chosen = BeatmapMirror._acquire_least_busy(urls, excluding=set())
                assert chosen is not None
                BeatmapMirror._release(chosen)
        except Exception as e:  # noqa: BLE001 — capture for the test
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"thread errors: {errors}"
    # Final state must be empty (every acquire paired with a release).
    assert BeatmapMirror._active == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_beatmap_mirror.py -v
```
Expected: 3 new tests fail — `AttributeError: type object 'BeatmapMirror' has no attribute '_release'`.

- [ ] **Step 3: Implement `_release`**

In `/var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py`, in the `BeatmapMirror` class, add this method directly after `_acquire_least_busy`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_beatmap_mirror.py -v
```
Expected: 3 new pass + 11 previous = 14 mirror tests, all green.

- [ ] **Step 5: Run full suite**

```
pytest tests/ -v
```
Expected: 20 total, all green.

- [ ] **Step 6: Commit**

```bash
git add osu_collector_gui.py tests/test_beatmap_mirror.py
git commit -m "BeatmapMirror: add _release + concurrency stress test"
```

---

## Task 4: Rewrite `download()` to use acquire/release

**Why:** This is the integration point — wire the new primitives into the actual download loop. The new shape is a while-loop that acquires least-busy, tries with retries, falls to the next mirror on failure.

**Files:**
- Modify: `osu_collector_gui.py:275-318` (`BeatmapMirror.download`)

- [ ] **Step 1: Read the current `download` method**

Run:
```
grep -n "def download" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py | head -5
```
Confirm the method is at the line range stated. Then read it (lines 275–318) to ensure the surrounding code matches the shape below before editing.

- [ ] **Step 2: Replace the entire `download` method**

In `/var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py`, replace the existing `download` method (lines 275–318) with:

```python
    def download(self, beatmapset_id: int, dest_dir: Path) -> Path | None:
        """Download .osz to dest_dir; return final path or None on failure.

        Uses least-busy mirror selection: each iteration picks the mirror
        with fewest active connections (excluding ones we've already
        tried this download), increments its active count for the
        duration of the request, then decrements on exit. Mirrors that
        fail at the TCP-connect layer get blacklisted process-wide so
        other parallel slots skip them.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        tried: set[str] = set()

        while True:
            base_url = self._acquire_least_busy(self.urls, excluding=tried)
            if base_url is None:
                break
            try:
                url = f"{base_url}/{beatmapset_id}"
                for attempt in range(HTTP_RETRIES):
                    try:
                        with self.session.get(url, stream=True,
                                              timeout=(DOWNLOAD_CONNECT_TIMEOUT_S,
                                                       DOWNLOAD_TIMEOUT_S),
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
                    except (requests.ConnectionError, requests.Timeout) as e:
                        # TCP-level failure: blacklist this mirror so other
                        # parallel slots skip it. Break the inner retry loop
                        # — retrying the same dead mirror wastes time.
                        self._mark_dead(base_url)
                        last_error = e
                        break
                    except requests.RequestException as e:
                        last_error = e
                        time.sleep(HTTP_BACKOFF_S * (attempt + 1))
                        continue
                tried.add(base_url)
            finally:
                self._release(base_url)

        # All mirrors tried and failed.
        if last_error:
            raise last_error
        return None
```

- [ ] **Step 3: Verify the file still imports**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python -c "from osu_collector_gui import BeatmapMirror; m = BeatmapMirror(); print('ok')"
```
Expected: `ok`.

- [ ] **Step 4: Run all tests**

```
pytest tests/ -v
```
Expected: 20 tests, all green. The existing `_urls_for_set`-targeted tests still pass because the method still exists at this point — Task 5 removes it.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py
git commit -m "BeatmapMirror.download: use acquire/release least-busy loop"
```

---

## Task 5: Remove `round_robin`, `_urls_for_set`, and obsolete tests

**Why:** Now that `download()` no longer touches them, the legacy round-robin code path is dead and should be removed for clarity.

**Files:**
- Modify: `osu_collector_gui.py` (`BeatmapMirror.__init__` + delete `_urls_for_set`)
- Modify: `tests/test_beatmap_mirror.py` (delete obsolete tests)

- [ ] **Step 1: Remove `round_robin` kwarg + `self.round_robin`**

In `/var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py`, find `BeatmapMirror.__init__` (currently around line 234). The current shape:

```python
    def __init__(self, primary: str = DEFAULT_MIRROR,
                 fallbacks: Iterable[str] = FALLBACK_MIRRORS,
                 round_robin: bool = False) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.urls = [primary, *fallbacks]
        self.round_robin = round_robin
```

Replace with:

```python
    def __init__(self, primary: str = DEFAULT_MIRROR,
                 fallbacks: Iterable[str] = FALLBACK_MIRRORS) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.urls = [primary, *fallbacks]
```

- [ ] **Step 2: Delete `_urls_for_set`**

In the same file, find `_urls_for_set` (currently around line 263–273) and delete the entire method. The block to delete:

```python
    def _urls_for_set(self, set_id: int) -> list[str]:
        """Return urls in order, with rotation if round_robin and skipping
        currently-blacklisted mirrors. If ALL mirrors are blacklisted,
        fall back to trying everything (the blacklist is just a hint)."""
        if self.round_robin and len(self.urls) > 1:
            offset = set_id % len(self.urls)
            ordered = self.urls[offset:] + self.urls[:offset]
        else:
            ordered = list(self.urls)
        alive = [u for u in ordered if not self._is_dead(u)]
        return alive if alive else ordered
```

- [ ] **Step 3: Delete obsolete tests in tests/test_beatmap_mirror.py**

Open `/var/home/red/Projects/Osu-Collector-GUI/tests/test_beatmap_mirror.py` and delete these three tests (they reference `round_robin` or `_urls_for_set`, both of which no longer exist):

```python
def test_round_robin_opt_in_rotates_by_set_id():
    ...

def test_default_keeps_static_primary_first_order():
    ...

def test_round_robin_with_single_mirror_is_no_op():
    ...
```

Also retarget the two remaining dead-mirror tests from `_urls_for_set` to `_acquire_least_busy`. Replace this test:

```python
def test_dead_mirror_is_filtered_out_of_urls_for_set():
    BeatmapMirror.reset_state()
    m = BeatmapMirror(primary="https://a", fallbacks=["https://b", "https://c"])
    BeatmapMirror._mark_dead("https://a")
    assert m._urls_for_set(0) == ["https://b", "https://c"]
    BeatmapMirror.reset_state()
```

With:

```python
def test_acquire_filters_dead_unless_only_option():
    BeatmapMirror._mark_dead("https://a")
    chosen = BeatmapMirror._acquire_least_busy(
        ["https://a", "https://b", "https://c"], excluding=set()
    )
    # "a" is dead, so least-busy among alive {b, c} picks b (tie-break).
    assert chosen == "https://b"
```

And replace this test:

```python
def test_all_dead_mirrors_still_returns_full_list_as_fallback():
    # If literally every mirror is blacklisted, we shouldn't refuse to
    # try at all — return them all and let the request layer surface
    # the failure naturally.
    BeatmapMirror.reset_state()
    m = BeatmapMirror(primary="https://a", fallbacks=["https://b"])
    BeatmapMirror._mark_dead("https://a")
    BeatmapMirror._mark_dead("https://b")
    assert m._urls_for_set(0) == ["https://a", "https://b"]
    BeatmapMirror.reset_state()
```

With:

```python
def test_acquire_falls_back_to_dead_when_all_blacklisted():
    # Every mirror is blacklisted. We shouldn't refuse to try — pick
    # the primary anyway (the blacklist is a hint, not a hard block).
    BeatmapMirror._mark_dead("https://a")
    BeatmapMirror._mark_dead("https://b")
    chosen = BeatmapMirror._acquire_least_busy(
        ["https://a", "https://b"], excluding=set()
    )
    assert chosen == "https://a"
```

Note the `reset_state()` calls are gone from the new tests — the autouse fixture in `conftest.py` handles that now.

- [ ] **Step 4: Verify imports + run all tests**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python -c "from osu_collector_gui import BeatmapMirror; print('ok')"
pytest tests/ -v
```
Expected: import prints `ok`. pytest: 11 mirror tests (was 14 minus the 3 deleted) + smoke + osdb + probe = 17 total green.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py tests/test_beatmap_mirror.py
git commit -m "BeatmapMirror: remove round_robin + _urls_for_set; retarget tests"
```

---

## Task 6: Version bump + CHANGELOG

**Files:**
- Modify: `osu_collector_gui.py:62` (APP_VERSION)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump APP_VERSION**

In `/var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py`, line 62 currently reads `APP_VERSION = "0.6.1"`. Change to:

```python
APP_VERSION = "0.6.2"
```

- [ ] **Step 2: Add CHANGELOG entry**

In `/var/home/red/Projects/Osu-Collector-GUI/CHANGELOG.md`, insert a new section at the top — after the `# Changelog` header and the blurb, BEFORE the existing `## [0.6.1]` section:

```markdown
## [0.6.2] — 2026-05-12

### Fixed

- **Downloads now spread load across mirrors intelligently.** Previously every parallel download slot started by trying catboy.best, so if catboy was rate-limiting the user's IP all 10 slots would each pay a full TCP-connect timeout (~10s × 3 retries) before falling back. Now `BeatmapMirror` picks the least-busy alive mirror for each new download — when catboy is healthy and fast its active count drops to 0 immediately so it stays preferred; when catboy stalls or its connections pile up, load shifts to nerinyan / osu.direct / beatconnect automatically. No magic numbers, no UI controls, no user-visible behavior changes when mirrors are working normally.

```

Note: one blank line between this section and `## [0.6.1]`.

- [ ] **Step 3: Verify version**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python -c "import osu_collector_gui; print(osu_collector_gui.APP_VERSION)"
```
Expected: `0.6.2`.

- [ ] **Step 4: Run tests**

```
pytest tests/ -v
```
Expected: 17 tests green.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py CHANGELOG.md
git commit -m "v0.6.2: least-busy mirror selection for parallel downloads"
```

---

## Task 7: Manual smoke (user-driven)

**Why:** Real network behavior can't be unit-tested. These checks verify the end-to-end behavior with actual mirrors.

- [ ] **Smoke 1: Healthy catboy — single mirror dominates**

Pre-condition: catboy.best is reachable from your IP. Verify quickly:

```
curl -sSL --max-time 10 -o /dev/null -w "catboy: %{http_code} %{time_total}s\n" https://catboy.best/d/1
```
Expected: `200` in well under 5s.

Launch the GUI:
```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python osu_collector_gui.py
```
Run a small collection (~50 sets) without `skip_already_imported`. Watch the log:

Expected: download lines flow smoothly. Roughly speaking, with catboy fast you should see filenames downloading at a steady pace. You won't see explicit per-mirror counts in the log (we didn't add UI for that), but you can `tcpdump` or `ss -t` to confirm if curious.

- [ ] **Smoke 2: Block catboy mid-batch**

While the same batch is mid-flight, deliberately break catboy. Open a terminal and:

```
# Block outbound traffic to catboy
sudo iptables -I OUTPUT -d 188.114.97.7 -j DROP  # catboy.best's IP — verify with `dig catboy.best`
```

(Substitute the actual IP from `dig +short catboy.best A`.)

Expected: the next download attempt to catboy times out at the TCP layer (~10s connect timeout), the mirror is marked dead, subsequent downloads proceed via nerinyan / osu.direct / beatconnect without further catboy attempts. Look for `[error <setid>: ...]` lines that contain `ConnectionError` or `Timeout` — one per parallel slot that hit catboy before the blacklist took effect.

After verifying the fallback worked, restore network:

```
sudo iptables -D OUTPUT -d 188.114.97.7 -j DROP
```

Within 60 seconds (the dead-cache TTL), catboy gets retried; if reachable, future downloads use it again.

- [ ] **Smoke 3: All mirrors unreachable**

Disconnect from the internet entirely (turn off wifi / unplug ethernet). Try a download.

Expected: each download tries every mirror, every connect times out, the worker raises `last_error` for that set and logs an `[error <setid>: ...]` line. No GUI crash, no hung worker. Re-connect, click Cancel if needed, GUI returns to ready.

- [ ] **All smoke tests pass — v0.6.2 ready for review/push decision**

---

## Self-Review

**Spec coverage** (vs. `docs/superpowers/specs/2026-05-12-least-busy-mirror-selection-design.md`):

- ✅ Consolidated `_state_lock` guarding `_dead_until` + new `_active` → Task 1
- ✅ `_acquire_least_busy(candidates, excluding) -> str | None` with tie-break + dead-fallback + None-on-all-excluded → Task 2
- ✅ `_release(url)` with pop-at-zero → Task 3
- ✅ `reset_state()` clearing both maps → Task 1
- ✅ `download()` rewritten with acquire/release while-loop → Task 4
- ✅ `round_robin` kwarg + `self.round_robin` + `_urls_for_set` removed → Task 5
- ✅ Lock-discipline invariant (HTTP outside lock) → implemented in Task 4's method body, where `session.get` runs outside the `with cls._state_lock` block
- ✅ All 9 unit tests from spec (6 acquire + 2 release + 1 concurrency) → Tasks 2 & 3
- ✅ 3 obsolete tests deleted, 2 dead-mirror tests retargeted → Task 5
- ✅ Conftest autouse fixture for state reset → Task 1
- ✅ Version bump + CHANGELOG → Task 6
- ✅ Manual smoke (healthy / blocked / offline) → Task 7

**Placeholder scan:** no TBD/TODO. Every step has either exact code or exact commands with expected output.

**Type consistency:**
- `_acquire_least_busy(candidates: list[str], excluding: set[str]) -> str | None` — used identically in tests (Task 2) and `download()` (Task 4) ✅
- `_release(url: str) -> None` — identical between definition (Task 3) and call site in `download()` finally block (Task 4) ✅
- `_state_lock`, `_active`, `_dead_until` — names match across Task 1 (definitions), Task 2 (test inspection), Task 3 (test inspection), Task 4 (callers) ✅
- `reset_state()` — defined Task 1, referenced from conftest fixture (Task 1) ✅
- `MIRROR_DEAD_TTL_S` — referenced by `_mark_dead` in Task 1; defined at module top (unchanged from v0.6.1) ✅
