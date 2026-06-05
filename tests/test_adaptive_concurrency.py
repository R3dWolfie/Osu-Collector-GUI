"""Adaptive per-mirror concurrency (AIMD): the cap grows on sustained
success, halves on a 429, and gates mirror selection."""
import osu_collector_gui as g
from osu_collector_gui import BeatmapMirror as B


def test_rate_limit_halves_cap_and_cools_down():
    B.reset_state()
    u = "https://m/d"
    B._limit[u] = 8
    cooldown = B.on_rate_limited(u, retry_after=30.0)
    assert cooldown == 30.0
    assert B._limit[u] == 4          # halved
    assert B._is_dead(u)             # cooling down
    B.on_rate_limited(u, None)       # default cooldown, halves again
    assert B._limit[u] == 2


def test_rate_limit_never_below_floor():
    B.reset_state()
    u = "https://m/d"
    B._limit[u] = g.PER_MIRROR_MIN
    B.on_rate_limited(u, None)
    assert B._limit[u] == g.PER_MIRROR_MIN


def test_success_probes_cap_upward_slowly():
    B.reset_state()
    u = "https://m/d"
    B._limit[u] = g.PER_MIRROR_START
    # Below the probe threshold: no change yet.
    for _ in range(g.PER_MIRROR_PROBE_EVERY - 1):
        B.on_success(u)
    assert B._limit[u] == g.PER_MIRROR_START
    # The Nth success bumps it by one.
    B.on_success(u)
    assert B._limit[u] == g.PER_MIRROR_START + 1


def test_success_caps_at_ceiling():
    B.reset_state()
    u = "https://m/d"
    B._limit[u] = g.PER_MIRROR_MAX
    for _ in range(g.PER_MIRROR_PROBE_EVERY * 2):
        B.on_success(u)
    assert B._limit[u] == g.PER_MIRROR_MAX


def test_acquire_respects_cap():
    B.reset_state()
    urls = ["https://a/d", "https://b/d"]
    B._limit["https://a/d"] = 1
    B._limit["https://b/d"] = 1
    # Fill mirror a to its cap of 1.
    first = B._acquire_least_busy(urls, excluding=set(), respect_caps=True)
    assert first == "https://a/d"
    # a is now full -> next pick must be b (not a again).
    second = B._acquire_least_busy(urls, excluding=set(), respect_caps=True)
    assert second == "https://b/d"
    # Both full -> None (caller waits), never over-subscribes.
    assert B._acquire_least_busy(urls, excluding=set(), respect_caps=True) is None
