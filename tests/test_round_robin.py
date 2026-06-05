"""Verify mirrors are used round-robin (1->2->3->4->1...) so load spreads
evenly across mirrors instead of every download hammering the primary."""
from unittest.mock import MagicMock

from osu_collector_gui import BeatmapMirror

VALID_OSZ = b"PK\x03\x04" + b"\x00" * 4000


class _Resp:
    def __init__(self): self.status_code = 200; self.headers = {}
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_content(self, chunk_size=0): yield VALID_OSZ


def test_next_start_increments_and_resets():
    BeatmapMirror.reset_state()
    assert [BeatmapMirror._next_start() for _ in range(3)] == [0, 1, 2]
    BeatmapMirror.reset_state()
    assert BeatmapMirror._next_start() == 0


def test_sequential_downloads_rotate_mirrors(tmp_path):
    BeatmapMirror.reset_state()
    m = BeatmapMirror()
    # Map a formatted request URL back to the mirror template it came from.
    prefixes = [(t.split("{id}")[0], t) for t in m.urls]
    seen: list[str] = []

    def fake_get(url, **kwargs):
        for prefix, template in prefixes:
            if url.startswith(prefix):
                seen.append(template)
                break
        return _Resp()

    m.session = MagicMock()
    m.session.get.side_effect = fake_get

    for sid in range(len(m.urls) * 2):
        m.download(sid, tmp_path)

    # Two full passes through the mirror list, in order.
    assert seen == m.urls + m.urls
