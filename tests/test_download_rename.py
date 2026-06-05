"""Verify BeatmapMirror.download handles an already-present .osz without the
Windows rename-onto-existing-file crash (WinError 183), and downloads cleanly
when the file is absent."""
from unittest.mock import MagicMock

from osu_collector_gui import BeatmapMirror


class _FakeResponse:
    def __init__(self, chunks, filename):
        self.status_code = 200
        self._chunks = chunks
        self.headers = {
            "content-disposition": f'attachment; filename="{filename}"'
        }

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=0):
        yield from self._chunks


def _mirror_with_response(resp):
    m = BeatmapMirror(primary="https://mirror")
    m.session = MagicMock()
    m.session.get.return_value = resp
    return m


def test_download_fresh_writes_file(tmp_path):
    resp = _FakeResponse([b"oszdata"], "123 Artist - Title.osz")
    m = _mirror_with_response(resp)

    out = m.download(123, tmp_path)

    assert out == tmp_path / "123 Artist - Title.osz"
    assert out.read_bytes() == b"oszdata"
    # No leftover .part file.
    assert list(tmp_path.glob("*.part")) == []


def test_download_skips_when_complete_file_exists(tmp_path):
    """A pre-existing non-empty .osz must not trigger a re-download or a
    rename onto the existing file (the WinError 183 path on Windows)."""
    existing = tmp_path / "123 Artist - Title.osz"
    existing.write_bytes(b"already-here")

    resp = _FakeResponse([b"new-body-should-not-be-written"],
                         "123 Artist - Title.osz")
    iter_spy = MagicMock(wraps=resp.iter_content)
    resp.iter_content = iter_spy
    m = _mirror_with_response(resp)

    out = m.download(123, tmp_path)

    assert out == existing
    # Body was never streamed, and the existing file is untouched.
    iter_spy.assert_not_called()
    assert existing.read_bytes() == b"already-here"
    assert list(tmp_path.glob("*.part")) == []
