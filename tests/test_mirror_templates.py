"""Custom-mirror template normalization + mirror-list assembly."""
from osu_collector_gui import BeatmapMirror, DEFAULT_MIRROR


def test_normalize_full_template_kept():
    assert BeatmapMirror.normalize_template(
        "https://m.example/api4/download/{id}"
    ) == "https://m.example/api4/download/{id}"


def test_normalize_base_url_gets_id_appended():
    assert BeatmapMirror.normalize_template("https://m.example/d") \
        == "https://m.example/d/{id}"
    assert BeatmapMirror.normalize_template("https://m.example/d/") \
        == "https://m.example/d/{id}"


def test_normalize_rejects_empty_and_non_http():
    assert BeatmapMirror.normalize_template("") is None
    assert BeatmapMirror.normalize_template("   ") is None
    assert BeatmapMirror.normalize_template("ftp://nope/d") is None
    assert BeatmapMirror.normalize_template("just text") is None


def test_extra_mirrors_are_preferred_and_deduped():
    extra = ["https://custom/d/{id}", DEFAULT_MIRROR]  # second dups a builtin
    m = BeatmapMirror(extra=extra)
    # Custom one comes first, the duplicate of the default isn't repeated.
    assert m.urls[0] == "https://custom/d/{id}"
    assert m.urls.count(DEFAULT_MIRROR) == 1


def test_default_mirror_set_includes_fixed_osu_direct_and_nekoha():
    m = BeatmapMirror()
    joined = " ".join(m.urls)
    assert "osu.direct/d/{id}" in joined
    assert "api.osu.direct" not in joined          # the broken host is gone
    assert "mirror.nekoha.moe/api4/download/{id}" in joined
