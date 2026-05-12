"""Verify BeatmapMirror URL selection."""
from osu_collector_gui import BeatmapMirror


def test_acquire_filters_dead_unless_only_option():
    BeatmapMirror._mark_dead("https://a")
    chosen = BeatmapMirror._acquire_least_busy(
        ["https://a", "https://b", "https://c"], excluding=set()
    )
    # "a" is dead, so least-busy among alive {b, c} picks b (tie-break).
    assert chosen == "https://b"


def test_acquire_falls_back_to_dead_when_all_blacklisted():
    # Every mirror is blacklisted. We shouldn't refuse to try — pick
    # the primary anyway (the blacklist is a hint, not a hard block).
    BeatmapMirror._mark_dead("https://a")
    BeatmapMirror._mark_dead("https://b")
    chosen = BeatmapMirror._acquire_least_busy(
        ["https://a", "https://b"], excluding=set()
    )
    assert chosen == "https://a"


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
