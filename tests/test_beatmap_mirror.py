"""Verify BeatmapMirror URL selection.

Default is round_robin=False (fast: catboy primary, others on error).
Pass round_robin=True to spread load across mirrors (politeness mode).
"""
from osu_collector_gui import BeatmapMirror


def test_round_robin_opt_in_rotates_by_set_id():
    m = BeatmapMirror(
        primary="https://a",
        fallbacks=["https://b", "https://c"],
        round_robin=True,
    )
    assert m._urls_for_set(0) == ["https://a", "https://b", "https://c"]
    assert m._urls_for_set(1) == ["https://b", "https://c", "https://a"]
    assert m._urls_for_set(2) == ["https://c", "https://a", "https://b"]
    assert m._urls_for_set(3) == ["https://a", "https://b", "https://c"]


def test_default_keeps_static_primary_first_order():
    m = BeatmapMirror(primary="https://a", fallbacks=["https://b", "https://c"])
    for sid in (0, 1, 7, 100):
        assert m._urls_for_set(sid) == ["https://a", "https://b", "https://c"]


def test_round_robin_with_single_mirror_is_no_op():
    m = BeatmapMirror(primary="https://only", fallbacks=[], round_robin=True)
    assert m._urls_for_set(0) == ["https://only"]
    assert m._urls_for_set(99) == ["https://only"]


def test_dead_mirror_is_filtered_out_of_urls_for_set():
    BeatmapMirror.reset_state()
    m = BeatmapMirror(primary="https://a", fallbacks=["https://b", "https://c"])
    BeatmapMirror._mark_dead("https://a")
    assert m._urls_for_set(0) == ["https://b", "https://c"]
    BeatmapMirror.reset_state()


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
