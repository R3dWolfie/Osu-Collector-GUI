# Skip already-imported beatmaps + configurable parallel downloads

**Date:** 2026-05-12
**Target version:** v0.6.0
**Status:** Design approved, pending implementation plan

## Motivation

osu!collector hosts collections that can contain 10k+ beatmaps (e.g. collection 17391 has ~11k maps). For users who already have a substantial lazer beatmap library, redownloading every .osz is wasteful — both bandwidth and time. The user reported this concretely: "if i for example put 17391 which is a HUGE 11k maps collection, i wanna skip maps i already have maybe? if possible? and just add them to the collection along with maps i downloaded?"

Two features fall out:

1. **Skip already-imported sets.** Before downloading, find out which beatmaps lazer already has and skip their .osz downloads. Crucially: still include those maps in the resulting lazer collection (the user expects "collection 17391" in lazer to contain all 11k maps, regardless of which were freshly downloaded).
2. **Configurable parallel downloads.** The current download worker is hardcoded to 4 concurrent .osz fetches (`DOWNLOAD_PARALLEL` at osu_collector_gui.py:86). Expose this in the GUI with a higher ceiling so users on fast connections can saturate their pipe.

## Approach

### Dedup data source: CM CLI probe

To know which beatmaps lazer has imported, we run Collection Manager CLI in `create` mode against the live realm:

```
cm.exe create -b <bids.txt> -o probe.osdb -l <realm_parent>
```

CM CLI loads lazer's BeatmapInfo database (via `-l`), enriches each beatmap_id it recognizes with full metadata (artist, title, diff, md5), and writes unresolved IDs into the .osdb's hash-only section. By parsing the resulting probe.osdb we get two things at once:

- **Skip list:** beatmap_ids lazer has → their set_ids → skip those .osz downloads
- **Updated md5s:** lazer's current md5 for each recognized beatmap (which may differ from osu!collector's md5 if the mapper updated the diff)

This approach was chosen over alternatives:

- **python-realm SDK:** would couple us to lazer's schema-version migrations and bloat the PyInstaller .exe by ~50 MB. v0.5.0's changelog documents how much pain went into making CM CLI the *only* realm reader; adding a second interpreter doubles that maintenance surface.
- **Filesystem-only scan:** doesn't match the chosen semantic ("anything imported in lazer"), only catches re-runs of the GUI itself.

The user picked **"anything imported in lazer"** as the dedup scope (over "anything in my collections" / "in target collection only" / "on disk").

### Set-level granularity

The user picked **"skip if any diff of the set is imported"** over **"skip only if all collection md5s for the set match"**. This is the aggressive option that maximizes bandwidth savings. False negatives (re-downloading a set lazer already has but with different md5s after a mapper update) are acceptable; we never falsely *omit* a needed map because collection composition always uses all md5s from the osu!collector API regardless of download status.

### Parallel downloads + mirror round-robin

Raise the configurable parallel-download ceiling to 32, default 4 (current value). Spinbox in the GUI mirroring the existing "Parallel imports" pattern (osu_collector_gui.py:1553 ff.).

`BeatmapMirror.download()` (osu_collector_gui.py:221) currently tries primary first, falls back on error. Change to per-request rotation: for each set_id, pick `self.urls[set_id % len(self.urls)]` as primary, others as fallbacks. Spreads load across catboy / nerinyan / osu.direct so high parallel counts don't hammer one mirror.

The user explicitly did **not** want per-file Range-splitting ("multi-mirror per .osz via HTTP Range") or resume support — agreed; .osz files are too small (5–30 MB typical) to justify splitting overhead, and resume on a 32-parallel batch is awkward.

## Architecture

Per-collection pipeline with both features enabled:

```
fetch_collection(cid, with_beatmap_details=True)
  → CollectionInfo with beatmaps[] populated
       ↓
PROBE
  • write probe-bids.txt to <realm_parent>/.oc-gui-tmp/
  • cm.exe create -b probe-bids.txt -o probe.osdb -l <realm_parent>
  • parse probe.osdb → ProbeResult { resolved: dict[int → BeatmapInfo],
                                     unresolved: set[str] }
  • build skipped_set_ids = { map.set_id for map in beatmaps
                              if map.beatmap_id in resolved }
       ↓
OsdbWriter.write(col_dir/<name>.osdb, info)
  • uses probe's md5 when bid is in resolved, else osu!collector's md5
  • all maps end up in .osdb regardless of skip status
       ↓
DOWNLOAD (ThreadPoolExecutor max_workers=job.download_parallel, 1..32)
  • skip submissions for set_ids in skipped_set_ids
  • BeatmapMirror picks per-set primary via round-robin
       ↓
AUTO-IMPORT (unchanged) — only downloaded sets
       ↓
MERGE into lazer collections (unchanged) — .osdb already contains all md5s
```

The probe step is the only new external call, runs once per collection (one wine invocation, ~5–10s).

## Components

### New

**`ProbeResult`** (dataclass)
```python
@dataclass
class ProbeResult:
    resolved: dict[int, BeatmapInfo]   # bid → metadata (with lazer's md5)
```

We only need to know which beatmap_ids lazer **does** have — anything not in `resolved` is implicitly "lazer doesn't have it". The .osdb hash-only section parsing is therefore unnecessary; the existing `OsdbReader.read()` is enough.

**`CmCliRunner.probe_imported_beatmaps(realm_path: Path, beatmap_ids: list[int]) → ProbeResult`**

Writes bids list to `<realm_parent>/.oc-gui-tmp/probe-bids.txt` (one per line), runs `cm.exe create -b <bids_file> -o <probe_osdb> -l <realm_parent>` with a 120s timeout, parses the resulting probe.osdb via `OsdbReader.read()`. Uses the same wine-sandbox-safe path convention as the existing snapshot/export step.

### Extended

(No changes needed to `OsdbReader` — `read()` already returns the resolved BeatmapInfo entries from probe.osdb's main collection section, which is all the probe needs.)

**`OsdbWriter.write` / `write_many`**

Add optional `prefer_md5_map: dict[int, str] | None` parameter. When set and a beatmap's `beatmap_id` is in the map, use the provided md5 instead of `BeatmapInfo.md5`. No behavior change when None.

**`BeatmapMirror.__init__(primary, fallbacks, round_robin: bool = True)`** and **`BeatmapMirror.download()`**

When `round_robin=True`, the per-call iteration order is `[urls[set_id % N], *other urls in declared order]` rather than the static `[primary, *fallbacks]`. Retries within a single download remain in the rotated order.

**`DownloadJob` (dataclass at osu_collector_gui.py:909)** — two new fields:
```python
skip_already_imported: bool = False
download_parallel: int = 4
```

**`DownloadWorker.run()` (osu_collector_gui.py:1030)**

After `fetch_collection`, before `generate_osdb`, run the probe if all gates pass:
- `job.skip_already_imported is True`
- `job.add_to_lazer_collections is True` (probe needs CM CLI which is part of this section)
- `job.cm_cli_command` is set
- `job.lazer_realm_path` exists

On failure of any of these, set `skipped_set_ids = set()` and `probe_md5_map = {}` and continue.

The download phase uses `max_workers=job.download_parallel` and filters submissions by `skipped_set_ids`. The progress bar still ticks once per skipped set so the bar finishes properly.

### UI changes (`MainWindow._build_ui` at osu_collector_gui.py:1553)

Two additions, both in existing sections:

1. **In "Lazer collections" group:** new checkbox **"Skip beatmapsets already imported"** (default off). Disabled with tooltip "Set up Collection Manager CLI above to enable" when `cm_cli_command` is unset.

2. **In the download settings row:** new spinbox **"Parallel downloads"** (1–32, default 4) next to the existing "Parallel imports" spinbox.

Both keys persisted in `_save_settings` / `_load_settings` (osu_collector_gui.py:1830, 1836). Defaults preserve current behavior.

### Wire-up in `MainWindow._on_start` (osu_collector_gui.py:2273)

When `skip_already_imported=True`, force `with_beatmap_details=True` regardless of `generate_osdb` (the probe needs per-map beatmap_ids). Read the new spinbox into `job.download_parallel`.

## Data flow

```
1. fetch_collection(cid, with_beatmap_details=True)
   → CollectionInfo with beatmaps[] = [BeatmapInfo(beatmap_id, set_id, md5, ...)]

2. PROBE  (gated)
   a. mkdir -p <realm_parent>/.oc-gui-tmp/
   b. write probe-bids.txt: one beatmap_id per line
   c. cm.exe create -b <bids_file> -o <probe.osdb> -l <realm_parent>  (timeout=120s)
   d. parse probe.osdb via OsdbReader.read()
        resolved: bid → BeatmapInfo  (from main collection section)
        anything not in resolved is implicitly "lazer doesn't have it"
   e. probe_md5_map = { bid: info.md5 for bid, info in resolved.items() }
   f. skipped_set_ids = { m.set_id for m in info.beatmaps
                          if m.beatmap_id in resolved }
   g. log: "[probe] lazer has K/N maps; skipping S/T sets"

3. OsdbWriter.write(col_dir/<safe_name>.osdb, info, prefer_md5_map=probe_md5_map)
   → .osdb contains ALL maps (downloaded + skipped) with the best available md5

4. DOWNLOAD
   ThreadPoolExecutor max_workers=job.download_parallel
   for set_id in info.beatmapset_ids:
     if set_id in skipped_set_ids:
       beatmap_progress.emit(done+1, total)
       log("[skip] set <id> already imported")
       continue
     submit(_download_one, set_id, col_dir)
   ... (rest of existing per-set handling unchanged)

5. AUTO-IMPORT (unchanged) — only fires for downloaded .osz files

6. MERGE into lazer collections (unchanged) — uses the .osdb from step 3
```

## Error handling

| Failure mode | Behavior |
|---|---|
| Probe CM CLI invocation raises (wine crash, exit≠0) | Log `[probe] failed: <err> — proceeding without dedup`; `skipped_set_ids = set()`; continue with full download. Never abort the run. |
| probe.osdb is 0 bytes or missing | Same as above. |
| probe.osdb parse fails | Same. |
| Probe resolves zero maps (fresh lazer, empty realm) | Legitimate. `skipped_set_ids = set()`; no warning, just download everything. |
| `realm_path` not set or doesn't exist | UI gate prevents the skip checkbox from being usable. At worker level, gracefully no-op the probe. |
| `cm_cli_command` not set | UI gate disables the skip checkbox. Worker-level guard logs and continues without dedup. |
| Wine sandbox can't write the bids file | The realm-parent path convention handles this (same trick as the existing snapshot at osu_collector_gui.py:1251). |
| Mirror returns 429 / 503 at high parallel count | Existing `HTTP_RETRIES=3` + `HTTP_BACKOFF_S=2` handle this. Round-robin spreads load so a single throttled mirror doesn't tank the run. |
| User cancels mid-probe | `_cancelled` is checked before probe starts and on next iteration; probe itself completes (CM CLI has its own 120s timeout — cancellation kicks in once it returns). |
| User cancels mid-download at 32 parallel | Existing cancel path (futures.cancel + pool shutdown) handles it. In-flight requests finish naturally (~30s worst case). |

**Critical invariant preserved:** the existing merge step is **fail-closed** (refuses to write if export fails — see osu_collector_gui.py:1267). The new probe step is **fail-open** by design. These differ deliberately: a failed export risks wiping the user's collections; a failed probe just costs bandwidth.

## Cleanup

The new tempfiles (`probe-bids.txt`, `probe.osdb`) live under `<realm_parent>/.oc-gui-tmp/`, which is already cleaned up in the `finally` block of `_merge_into_lazer` (osu_collector_gui.py:1373). One extra `unlink(missing_ok=True)` for the bids file; the existing `rmtree(tmp_dir)` covers probe.osdb.

If the user runs with `skip_already_imported=True` but `add_to_lazer_collections=False`, the probe still runs (skip is independent of merge in principle) — the tempdir is created and torn down in the same per-collection scope. **Open decision:** should skip be gated on add_to_lazer_collections, or work independently? Resolved: **gate it**. Without the merge step, there's no way to attribute the skipped maps to a collection in lazer; the skip would just mean "I downloaded fewer .osz files and got a worse outcome". So the UI keeps them coupled.

## Settings persistence

Two new keys in `~/.config/osu-collector-gui/settings.json` (Linux) / `%APPDATA%\osu-collector-gui\settings.json` (Windows):

```json
{
  "skip_already_imported": false,
  "download_parallel": 4
}
```

Both default to current behavior. Loaded in `_load_settings` (osu_collector_gui.py:1830) with `.get(key, default)`.

## Testing

**Unit-testable in isolation:**

- `OsdbReader.read_full` against a known probe.osdb fixture with both main and hash-only sections populated
- `OsdbWriter.write` with `prefer_md5_map={}` (must equal current behavior)
- `OsdbWriter.write` with a populated `prefer_md5_map` (verify substitution at the right beatmap_id)
- `BeatmapMirror.download` round-robin: with mocked `requests.Session`, verify that two different set_ids hit different primary URLs

**Integration tests (require a real CM CLI + a fixture realm):**

- Empty realm: probe returns 0 resolved, all 0 unresolved → no skips
- Realm with 5 imported sets, collection has 3 of those + 2 new: probe resolves the 3, skip set = those 3, download set = the 2 new
- Realm with stale mapper md5 (same beatmap_id, different md5 in realm vs. collection): probe resolves the bid → uses realm's md5 → set is skipped (matches user's chosen aggressive semantic)
- Probe failure (point `cm_cli_command` at `/bin/false`): worker logs the failure and falls through to full-download path

**Smoke test:**

- Run collection 17391 (the user's actual ~11k case) end-to-end against a lazer install with substantial existing library. Verify: download count is materially smaller than 11k, final lazer collection contains all 11k maps, no realm corruption.

## Out of scope

- Per-file Range splitting (multi-mirror per .osz)
- Resume of interrupted downloads
- Live aggregate throughput / MB-per-second display
- Smarter per-mirror health detection (auto-shift load off a slow mirror)

These were considered and explicitly declined during brainstorming.

## Open questions resolved during brainstorming

| Question | Resolution |
|---|---|
| What does "already have" mean? | Anything imported in lazer (not just in collections, not just on disk) |
| Set-level granularity for skip? | Aggressive: skip if any diff of the set is imported |
| Probe via python-realm or CM CLI? | CM CLI — reuses infra, avoids schema-version coupling |
| Per-file multi-mirror Range splitting? | No — .osz too small, mirror byte-identity not guaranteed |
| Resume of interrupted downloads? | No |
| Should skip work when add_to_lazer_collections is off? | No — gate them together |
| Parallel download cap? | 32, default 4 (current behavior preserved) |
