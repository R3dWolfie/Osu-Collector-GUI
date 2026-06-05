"""Verify mirrors are used round-robin (1->2->3->4->1...) so load spreads
evenly across mirrors instead of every download hammering the primary."""
from unittest.mock import MagicMock

from osu_collector_gui import BeatmapMirror


class _Resp:
    def __init__(self): self.status_code = 200; self.headers = {}
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_content(self, chunk_size=0): yield b"x"


def _base(url: str) -> str:
    return url.rsplit("/", 1)[0]


def test_next_start_increments_and_resets():
    BeatmapMirror.reset_state()
    assert [BeatmapMirror._next_start() for _ in range(3)] == [0, 1, 2]
    BeatmapMirror.reset_state()
    assert BeatmapMirror._next_start() == 0


def test_sequential_downloads_rotate_mirrors(tmp_path):
    BeatmapMirror.reset_state()
    m = BeatmapMirror()
    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(_base(url))
        return _Resp()

    m.session = MagicMock()
    m.session.get.side_effect = fake_get

    for sid in range(len(m.urls) * 2):
        m.download(sid, tmp_path)

    # Two full passes through the mirror list, in order.
    assert seen == m.urls + m.urls
