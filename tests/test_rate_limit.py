"""Verify rate-limited mirrors (HTTP 429/403) are blacklisted and skipped
rather than retried, and that Retry-After parsing is sane."""
from unittest.mock import MagicMock

from osu_collector_gui import BeatmapMirror, _parse_retry_after


def test_parse_retry_after_seconds():
    assert _parse_retry_after("120") == 120.0


def test_parse_retry_after_caps_absurd_values():
    assert _parse_retry_after("999999", cap=600.0) == 600.0


def test_parse_retry_after_none_and_garbage():
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("soon-ish") is None


class _Resp:
    def __init__(self, status, body=b"", headers=None, filename="x.osz"):
        self.status_code = status
        self._body = body
        self.headers = {"content-disposition": f'attachment; filename="{filename}"'}
        if headers:
            self.headers.update(headers)

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))
    def iter_content(self, chunk_size=0):
        yield self._body


def test_rate_limited_mirror_is_blacklisted_and_falls_through(tmp_path):
    """A 429 on the primary mirror must blacklist it (honouring Retry-After)
    and download succeeds from the next mirror — without retrying the
    rate-limited one."""
    m = BeatmapMirror()
    primary = m.urls[0]
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.startswith(primary):
            return _Resp(429, headers={"Retry-After": "180"})
        return _Resp(200, body=b"osz-bytes")

    m.session = MagicMock()
    m.session.get.side_effect = fake_get

    out = m.download(123, tmp_path)

    assert out is not None and out.read_bytes() == b"osz-bytes"
    # Primary was tried exactly once (no wasteful retries) and is now dead.
    assert sum(1 for u in calls if u.startswith(primary)) == 1
    assert BeatmapMirror._is_dead(primary)
