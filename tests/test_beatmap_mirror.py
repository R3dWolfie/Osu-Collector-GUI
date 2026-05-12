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
