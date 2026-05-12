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
