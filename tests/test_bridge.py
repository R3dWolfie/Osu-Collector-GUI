"""Tests for the Qt-free web bridge layer (parsing, settings state, the
Downloader event shim). These run without a webview or any GUI deps."""
import osu_collector_gui as g


def test_parse_ids_accepts_urls_and_bare_ids():
    text = "https://osucollector.com/collections/17391\n42\n  99 "
    assert g._parse_ids(text) == [17391, 42, 99]


def test_parse_ids_splits_commas_and_spaces():
    assert g._parse_ids("1, 2 3,4") == [1, 2, 3, 4]


def test_parse_ids_dedupes_preserving_order():
    assert g._parse_ids("5\n5\n3\n5") == [5, 3]


def test_parse_ids_ignores_junk():
    assert g._parse_ids("not-an-id\nhttps://example.com/x\n") == []


def test_normalize_cm_handles_str_and_list():
    assert g._normalize_cm(["flatpak", "run", "wine"]) == ["flatpak", "run", "wine"]
    assert g._normalize_cm('flatpak run "org.winehq.Wine"') == [
        "flatpak", "run", "org.winehq.Wine"
    ]
    assert g._normalize_cm("") == []
    assert g._normalize_cm(None) == []


def test_get_state_has_expected_shape():
    api = g.JsApi()
    st = api.get_state()
    for key in ("version", "theme", "output_dir", "target", "settings",
                "detected", "labels"):
        assert key in st
    assert st["version"] == g.APP_VERSION
    # Defaults are sensible without any saved settings.
    assert st["theme"] in ("dark", "light")
    assert st["labels"]["no_merge"] == g.target_combo_no_merge_label()


def test_downloader_emits_through_callback():
    events = []
    job = g.DownloadJob(collection_ids=[], output_dir=g.Path("/tmp"))
    d = g.Downloader(job, lambda name, payload: events.append((name, payload)))
    d._log("hello")
    d._beatmap_progress(2, 10)
    d._collection_started(1, 3, "Pool", 50)
    d._batch_finished(3, 3)
    names = [e[0] for e in events]
    assert names == ["log", "beatmap_progress", "collection_started",
                     "batch_finished"]
    assert events[0][1] == {"line": "hello"}
    assert events[1][1] == {"current": 2, "total": 10}
    assert events[2][1]["name"] == "Pool"


def test_start_rejects_when_no_ids():
    api = g.JsApi()
    res = api.start({"ids_text": "garbage", "settings": {}})
    assert res["ok"] is False
    assert "No valid" in res["error"]
