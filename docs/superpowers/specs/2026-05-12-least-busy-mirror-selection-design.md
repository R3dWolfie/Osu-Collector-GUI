# Least-busy mirror selection

**Date:** 2026-05-12
**Target version:** v0.6.2 (or v0.7.0 if bundled with the UI work)
**Status:** Design approved, pending implementation plan

## Motivation

The user reported: "really slow" downloads. Investigation traced this to catboy.best (the primary mirror) rate-limiting their IP. The default mirror order was "catboy first, fall back to nerinyan/osu.direct/beatconnect on failure." Every parallel download slot independently rediscovered catboy was blocking — wasting ~10 seconds of TCP-connect timeout per slot per attempt before falling through.

A previous patch (v0.6.1) added a shared dead-mirror blacklist: when one slot detects a TCP failure, the mirror is marked dead for 60 seconds so other slots skip it. This helps, but is reactive — the *first* slot to hit a blocked mirror still pays the full timeout, and the static "catboy primary, others fallback" ordering means every slot starts by trying catboy until the first one fails.

The user's framing: "use mirrors in parallel to reduce traffic per mirror so it won't get blocked." Confirmed policy preference: **prefer fastest mirror when it's healthy, only spread load away from it when it gets busy/slow**.

This is exactly the behavior of a **least-busy load balancer**: pick the mirror with the fewest currently-active downloads, with ties broken by mirror declaration order (so catboy wins on cold start). Catboy stays preferred while it's responding fast (its active count drops back to 0 immediately as downloads complete); load shifts away from catboy when its connections pile up or stall.

## Approach

Replace the static round-robin / primary-first URL ordering in `BeatmapMirror` with dynamic least-busy selection backed by a process-wide active-connection counter.

For each download request, atomically:

1. Among the configured mirrors not in the dead-cache and not in `tried`, pick the one with the lowest active count.
2. If everything alive is excluded, fall back to picking from the full candidate list (even dead ones). Failure surfaces naturally rather than refusing to try.
3. If everything is excluded, return None and let the outer loop give up.
4. Tie-break by index in the declared mirror order (catboy first).
5. Increment its active count.

Then perform the HTTP request **outside the lock**, and decrement the count in a `finally` block regardless of outcome.

The existing dead-mirror blacklist remains. Its lock merges with the new active-count lock into a single `_state_lock`.

Alternative approaches considered and declined:

- **Hedged requests** (start primary, after 300ms start secondary, take first to respond). Lowest individual-download latency, but explicitly *increases* total traffic during hedge windows — opposite of the user's stated goal.
- **Strict per-mirror cap with queueing** (e.g. max 3 concurrent per mirror, the rest queue). Predictable load shape, but requires picking a cap value out of thin air, and introduces explicit queue latency for downloads beyond the cap.

The least-busy approach beats both: no magic numbers, no extra queueing, self-balancing under all observable mirror conditions.

## Architecture

```
DownloadWorker spawns N parallel slots
  ↓
each calls mirror.download(set_id, dest_dir)
  ↓
download() loop:
    while not exhausted:
        base_url = _acquire_least_busy(self.urls, excluding=tried)
        if base_url is None: break
        try:
            for attempt in range(HTTP_RETRIES):
                try:
                    session.get(base_url/set_id, timeout=(connect, read))
                    on 404: return None
                    on success: write .part → rename → return dest
                except (ConnectionError, Timeout):
                    _mark_dead(base_url); last_error = e; break
                except RequestException:
                    last_error = e; sleep(backoff); continue
            tried.add(base_url)
        finally:
            _release(base_url)
    if last_error: raise last_error
    return None
```

Lock discipline: `_state_lock` is held **only** during pick + increment in `_acquire_least_busy` and during decrement in `_release`. Never held during HTTP I/O.

## Components

### Changed in `osu_collector_gui.py` (`BeatmapMirror` class only)

| Change | Detail |
|---|---|
| Class state | `_dead_until: dict[str, float]` and new `_active: dict[str, int]` share a single `_state_lock`. The old `_dead_lock` is removed. |
| `__init__` | Remove the `round_robin: bool = False` kwarg. Remove `self.round_robin`. |
| `_urls_for_set` | Delete. Replaced by `_acquire_least_busy` + `_release`. |
| `_is_dead` / `_mark_dead` | Same signatures; switch the lock from `_dead_lock` to `_state_lock`. |
| `reset_dead_mirrors` | Rename to `reset_state`; clear both `_dead_until` and `_active`. |
| `_acquire_least_busy` (new) | See signature below. |
| `_release` (new) | See signature below. |
| `download()` | Rewrite outer loop using acquire/release pattern. Inner retry loop unchanged. |

### New method signatures

```python
@classmethod
def _acquire_least_busy(cls, candidates: list[str],
                       excluding: set[str]) -> str | None:
    """Atomically pick the least-busy alive mirror among candidates
    not in excluding, and increment its active count. Tie-break by
    index in candidates. Falls back to dead mirrors if every alive
    candidate is excluded. Returns None only when every candidate
    is excluded."""

@classmethod
def _release(cls, url: str) -> None:
    """Decrement active count; pop the entry if it reaches 0.
    Must pair with a successful _acquire_least_busy call."""

@classmethod
def reset_state(cls) -> None:
    """Clear dead-cache and active counts. Used by tests + manual reset."""
```

### Removed

- Constructor kwarg `round_robin`
- Instance attribute `self.round_robin`
- Method `_urls_for_set`
- Class lock `_dead_lock` (folded into `_state_lock`)
- Helper `reset_dead_mirrors` (renamed to `reset_state`)

### Callers checked

`BeatmapMirror(...)` is constructed in exactly one place: `DownloadWorker.__init__` (around osu_collector_gui.py:1035). It currently passes only `primary=job.mirror_url`. Removing `round_robin` is safe — no caller passes it.

## Data flow

```
1. User clicks Start. DownloadWorker spawns N=10 parallel slots in
   a ThreadPoolExecutor.

2. Each slot independently calls mirror.download(set_id, col_dir).

3. download() outer loop:
   tried = set()
   base_url = _acquire_least_busy(self.urls, excluding=set())
       # cold start: catboy wins tie-break, _active = {catboy: 1}
       # second slot: catboy at 1, nerinyan at 0 → nerinyan picked
       # third slot: catboy at 1, nerinyan at 1, osu.direct at 0 → osu.direct
       # fourth slot: ... → beatconnect
       # fifth slot: all at 1 → catboy (tie-break)
       # subsequent slots: whoever finished first has count back to 0

4. Inside try block:
   - HTTP_RETRIES attempts with backoff
   - on ConnectionError/Timeout: mark mirror dead, break to outer
   - on 404: return None (beatmap genuinely missing)
   - on success: write file, return dest

5. Finally: _release(base_url) — count decrements.

6. If we broke out (mirror failed), tried.add(base_url) and loop.
   Next acquire picks from the remaining mirrors, again least-busy.

7. If all mirrors tried and last_error is set: raise. Worker logs
   the error per-set and moves on (existing behavior).
```

## Error handling

| Failure mode | Behavior |
|---|---|
| `ConnectionError` or `Timeout` from `session.get` | Mark mirror dead (60s TTL), break inner retry loop, add to `tried`, continue with next mirror via outer loop. |
| Other `RequestException` (5xx, mid-stream reset, etc.) | Retry within the same mirror up to `HTTP_RETRIES` times with `HTTP_BACKOFF_S * (attempt+1)` linear backoff, then add to `tried` and continue. |
| HTTP 404 | Return `None` immediately. The beatmap is genuinely missing from this mirror; don't try others, don't mark dead. Same as today. |
| All mirrors exhausted | `raise last_error`. Worker catches at the call site and logs per-set. |
| Exception inside `try` block (e.g. file write failure) | `finally: _release(base_url)` always runs. Counter is never leaked. |
| Race between two threads calling `_acquire_least_busy` | Serialized by `_state_lock`. First sees state, picks, increments, releases lock. Second sees updated state and picks the next-least-busy. |

## Cleanup / migration

- The `round_robin` constructor kwarg is removed. There is exactly one caller in the codebase (`DownloadWorker.__init__`) and it doesn't pass the kwarg, so removal is safe. No external callers exist.
- Tests using `round_robin` (currently in `tests/test_beatmap_mirror.py`) must be deleted or rewritten — see Testing.
- No new settings, no new GUI, no CHANGELOG-worthy user-facing change beyond "downloads spread load across mirrors more intelligently." Will appear as one bullet under "Improved" / "Fixed" in the v0.6.2 entry.

## Testing

Unit-testable in isolation (pure logic, no HTTP):

1. `test_acquire_picks_primary_on_cold_state` — clear state, candidates `[a,b,c]`, excluding `{}` → returns `a`. `_active == {"a": 1}`.
2. `test_acquire_breaks_ties_by_url_order` — set `_active = {a:1, b:1, c:1}` → returns `a` (tie + lowest index). After acquire, `_active["a"] == 2`.
3. `test_acquire_picks_least_busy_when_not_tied` — `_active = {a:3, b:1, c:2}` → returns `b`. `_active["b"] == 2`.
4. `test_acquire_excludes_listed_urls` — `_active` empty, `excluding={"a"}` → returns `b` (cold-state tie-break, but `a` is excluded so `b` wins).
5. `test_acquire_falls_back_to_dead_when_alive_empty` — mark `a` dead, `excluding={"b","c"}` → returns `a` despite being dead. The "alive set" is empty, so fallback kicks in.
6. `test_acquire_returns_none_when_everything_excluded` — `excluding={"a","b","c"}` → returns `None`. Outer loop ends.
7. `test_release_decrements_and_pops_at_zero` — acquire `a` twice (count=2), release once (count=1), release again (count=0, key absent from `_active`).
8. `test_release_preserves_other_mirrors` — acquire `a`, acquire `b`, release `a` → `_active == {"b": 1}`.
9. `test_concurrent_acquire_release_does_not_leak_or_corrupt` — spawn 50 threads, each acquires + releases 100 times. Final `_active` is empty, no negative counts, no KeyError exceptions.

Test fixture: `reset_state()` called in setup/teardown for every test (the global state is class-level and bleeds between tests otherwise).

Removed tests (round-robin gone):

- `test_round_robin_opt_in_rotates_by_set_id` — delete
- `test_default_keeps_static_primary_first_order` — replace with the new `test_acquire_picks_primary_on_cold_state`
- `test_round_robin_with_single_mirror_is_no_op` — replace with a new test verifying single-mirror always picks the only mirror
- `test_dead_mirror_is_filtered_out_of_urls_for_set` — retarget to `_acquire_least_busy` (becomes `test_acquire_filters_dead_unless_only_option`)
- `test_all_dead_mirrors_still_returns_full_list_as_fallback` — retarget similarly

Manually verifiable post-merge: download a small collection (~50 sets), observe in the log that all 4 mirrors get a roughly proportionate number of hits (vs. v0.6.1 which would hit catboy almost exclusively until it failed). Deliberately break a mirror (block via firewall) mid-batch and verify the rest finish through the other three.

## Concurrency invariant

`_state_lock` is held **only** during the pick + increment inside `_acquire_least_busy` and during the decrement inside `_release`. The HTTP request itself runs without holding the lock. If any future change accidentally holds the lock across the HTTP call, all parallel downloads serialize on it and we get zero speedup over single-threaded. The implementation must preserve this.

## Out of scope

- **Hedged requests / racing multiple mirrors** for a single .osz — declined; doubles traffic.
- **Per-mirror cap with explicit queueing** — declined; less adaptive than least-busy.
- **Mirror health scoring beyond active count** — out of scope. Could be a future iteration if least-busy proves insufficient (e.g. tracking per-mirror EWMA throughput and weighting accordingly).
- **User-facing controls for mirror behavior** — out of scope, and explicitly contrary to the user's wish for a simpler UI.

## Open questions resolved during brainstorming

| Question | Resolution |
|---|---|
| Order of v0.7 work (UI vs mirrors) | Mirrors first, UI separate spec. |
| Distribution policy | Prefer fastest, only spread when needed (self-balancing). |
| Approach | Least-busy mirror selection. Hedged requests + strict cap declined. |
| Keep `round_robin` as opt-in? | No — least-busy subsumes it entirely. |
| Add UI controls? | No — user explicitly wants fewer UI controls, not more. |
