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


def test_sayobot_hard_capped_at_one():
    """Sayobot (slow CN CDN) is hard-capped to a single concurrent download,
    regardless of the adaptive cap — both its full and no-video templates."""
    assert g._mirror_hard_cap("https://dl.sayobot.cn/beatmaps/download/full/{id}") == 1
    assert g._mirror_hard_cap("https://dl.sayobot.cn/beatmaps/download/novideo/{id}") == 1
    assert g._mirror_hard_cap("https://catboy.best/d/{id}") == g.PER_MIRROR_MAX

    B.reset_state()
    sayo = "https://dl.sayobot.cn/beatmaps/download/full/{id}"
    # One slot only — a second acquire must be refused even though the adaptive
    # start (2) would otherwise allow it.
    assert B._acquire_least_busy([sayo], excluding=set(), respect_caps=True) == sayo
    assert B._acquire_least_busy([sayo], excluding=set(), respect_caps=True) is None


def test_sayobot_cap_never_probes_upward():
    """on_success must not let sayobot's cap climb past its hard ceiling of 1."""
    B.reset_state()
    sayo = "https://dl.sayobot.cn/beatmaps/download/full/{id}"
    B._limit[sayo] = 1
    for _ in range(g.PER_MIRROR_PROBE_EVERY * 3):
        B.on_success(sayo)
    assert B._limit[sayo] == 1   # hard ceiling of 1, never grows
