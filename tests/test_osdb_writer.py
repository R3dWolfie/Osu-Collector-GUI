"""Verify OsdbWriter.write honors the prefer_md5_map override."""
from osu_collector_gui import OsdbWriter, OsdbReader, CollectionInfo, BeatmapInfo


def _one_collection_with(beatmaps):
    return CollectionInfo(
        id=1, name="test", uploader="me",
        beatmap_count=len(beatmaps), beatmaps=beatmaps,
    )


def test_write_without_prefer_md5_map_preserves_original_md5(tmp_path):
    info = _one_collection_with([
        BeatmapInfo(beatmap_id=42, set_id=100, md5="aaa",
                    artist="A", title="T", diff_name="D"),
    ])
    dest = tmp_path / "out.osdb"
    OsdbWriter.write(dest, info)

    [parsed] = OsdbReader.read(dest)
    [bm] = [b for b in parsed.beatmaps if b.beatmap_id == 42]
    assert bm.md5 == "aaa"


def test_write_with_prefer_md5_map_overrides_md5(tmp_path):
    info = _one_collection_with([
        BeatmapInfo(beatmap_id=42, set_id=100, md5="aaa",
                    artist="A", title="T", diff_name="D"),
    ])
    dest = tmp_path / "out.osdb"
    OsdbWriter.write(dest, info, prefer_md5_map={42: "bbb"})

    [parsed] = OsdbReader.read(dest)
    [bm] = [b for b in parsed.beatmaps if b.beatmap_id == 42]
    assert bm.md5 == "bbb"


def test_write_with_prefer_md5_map_falls_back_for_missing_bid(tmp_path):
    info = _one_collection_with([
        BeatmapInfo(beatmap_id=42, set_id=100, md5="aaa",
                    artist="A", title="T", diff_name="D"),
        BeatmapInfo(beatmap_id=99, set_id=200, md5="ccc",
                    artist="A", title="T", diff_name="D"),
    ])
    dest = tmp_path / "out.osdb"
    OsdbWriter.write(dest, info, prefer_md5_map={42: "bbb"})

    [parsed] = OsdbReader.read(dest)
    md5_by_id = {b.beatmap_id: b.md5 for b in parsed.beatmaps if b.beatmap_id}
    assert md5_by_id[42] == "bbb"
    assert md5_by_id[99] == "ccc"
