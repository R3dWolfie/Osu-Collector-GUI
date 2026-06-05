"""A mirror that answers 200 with a non-.osz body (Cloudflare / rate-limit
page) or a truncated file must be rejected and another mirror tried — this
is what caused osu!lazer's "Beatmap import failed" spam."""
from unittest.mock import MagicMock

from osu_collector_gui import BeatmapMirror

VALID_OSZ = b"PK\x03\x04" + b"\x00" * 4000   # ZIP magic + plausible size


class _Resp:
    def __init__(self, status, body, content_length=None, filename="x.osz"):
        self.status_code = status
        self._body = body
        self.headers = {"content-disposition": f'attachment; filename="{filename}"'}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def iter_content(self, chunk_size=0): yield self._body


def _mirror(side_effect):
    m = BeatmapMirror()
    m.session = MagicMock()
    m.session.get.side_effect = side_effect
    return m


def test_non_osz_200_is_rejected_and_next_mirror_used(tmp_path):
    BeatmapMirror.reset_state()
    primary = BeatmapMirror().urls[0]
    primary_prefix = primary.split("{id}")[0]

    def fake_get(url, **k):
        if url.startswith(primary_prefix):
            return _Resp(200, b"<html>rate limited</html>" + b" " * 4000)
        return _Resp(200, VALID_OSZ)

    m = _mirror(fake_get)
    out = m.download(7, tmp_path)
    assert out is not None
    assert out.read_bytes() == VALID_OSZ
    # The garbage-serving mirror got cooled down.
    assert BeatmapMirror._is_dead(primary)


def test_truncated_download_is_rejected(tmp_path):
    BeatmapMirror.reset_state()
    primary = BeatmapMirror().urls[0]
    primary_prefix = primary.split("{id}")[0]

    def fake_get(url, **k):
        if url.startswith(primary_prefix):
            # Claims 99999 bytes but only sends VALID_OSZ -> truncated.
            return _Resp(200, VALID_OSZ, content_length=99999)
        return _Resp(200, VALID_OSZ)

    m = _mirror(fake_get)
    out = m.download(8, tmp_path)
    assert out is not None and out.read_bytes() == VALID_OSZ


def test_all_mirrors_garbage_returns_none(tmp_path):
    BeatmapMirror.reset_state()

    def fake_get(url, **k):
        return _Resp(200, b"not a zip" * 500)

    m = _mirror(fake_get)
    # Every mirror serves garbage -> no file, but it must not crash or
    # hand a bad file back.
    try:
        out = m.download(9, tmp_path)
    except Exception:
        out = None
    assert out is None
    assert not any(tmp_path.glob("*.osz"))
