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
    BeatmapMirror.reset_dead_mirrors()
    m = BeatmapMirror(primary="https://a", fallbacks=["https://b", "https://c"])
    BeatmapMirror._mark_dead("https://a")
    assert m._urls_for_set(0) == ["https://b", "https://c"]
    BeatmapMirror.reset_dead_mirrors()


def test_all_dead_mirrors_still_returns_full_list_as_fallback():
    # If literally every mirror is blacklisted, we shouldn't refuse to
    # try at all — return them all and let the request layer surface
    # the failure naturally.
    BeatmapMirror.reset_dead_mirrors()
    m = BeatmapMirror(primary="https://a", fallbacks=["https://b"])
    BeatmapMirror._mark_dead("https://a")
    BeatmapMirror._mark_dead("https://b")
    assert m._urls_for_set(0) == ["https://a", "https://b"]
    BeatmapMirror.reset_dead_mirrors()
