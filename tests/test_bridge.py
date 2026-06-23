"""Tests for the Qt-free web bridge layer (parsing, settings state, the
Downloader event shim). These run without a webview or any GUI deps."""
from unittest.mock import MagicMock

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


def test_detected_state_honors_manual_realm_override(tmp_path):
    """A manually-set client.realm that exists must show as detected, even
    when auto-detection wouldn't have found it (regression: the panel used
    to report 'not found' for valid manual paths)."""
    realm = tmp_path / "client.realm"
    realm.write_bytes(b"x" * 16)
    api = g.JsApi()
    api._settings["lazer_realm_path"] = str(realm)
    det = api._detected_state()
    assert det["realm_detected"] is True
    assert det["realm_path"] == str(realm)
    # And it propagates into the full state the frontend renders.
    assert api.get_state()["detected"]["realm_detected"] is True


def test_detected_state_rejects_nonexistent_manual_realm(tmp_path, monkeypatch):
    """A manual path that doesn't exist must NOT be reported as detected.
    Force auto-detection to find nothing so we isolate the manual-path logic
    (the test host may have a real lazer realm at the default location)."""
    monkeypatch.setattr(g, "_default_lazer_realm_path",
                        lambda: tmp_path / "nope.realm")
    api = g.JsApi()
    api._settings["lazer_realm_path"] = "/no/such/place/client.realm"
    assert api._detected_state()["realm_detected"] is False


def test_failed_sets_are_retried_not_lost(tmp_path, monkeypatch):
    """A set that fails its first download on a transient mirror error must be
    retried and recovered, not abandoned (the bug: failures were just logged)."""
    job = g.DownloadJob(
        collection_ids=[1], output_dir=tmp_path,
        download_beatmaps=True, auto_import=False, generate_osdb=False,
        add_to_lazer_collections=False, skip_already_imported=False,
    )
    d = g.Downloader(job, lambda name, payload: None)

    info = g.CollectionInfo(id=1, name="T", uploader="R3D",
                            beatmap_count=2, beatmapset_ids=[101, 102])
    d.api = MagicMock()
    d.api.fetch_collection.return_value = info

    attempts: dict[int, int] = {}

    def fake_download(set_id, dest_dir, should_cancel=None):
        attempts[set_id] = attempts.get(set_id, 0) + 1
        if set_id == 101 and attempts[101] == 1:
            raise g.requests.HTTPError("429 rate limited")  # transient first time
        p = dest_dir / f"{set_id}.osz"
        p.write_bytes(b"PK\x03\x04")
        return p

    d.mirror = MagicMock()
    d.mirror.download.side_effect = fake_download
    monkeypatch.setattr(g.time, "sleep", lambda *a, **k: None)  # no real cooldown wait

    d.run()

    assert attempts[101] == 2, "the failed set should have been retried once"
    col_dir = tmp_path / "1 - T"
    assert (col_dir / "101.osz").exists(), "retried set must end up downloaded"
    assert (col_dir / "102.osz").exists()


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


def test_version_tuple_and_compare():
    assert g._version_tuple("v1.2.3") == (1, 2, 3)
    assert g._version_tuple("2.0") == (2, 0)
    assert g._is_newer("1.1.0", "1.0.0") is True
    assert g._is_newer("1.0.1", "1.0.0") is True
    assert g._is_newer("1.0.0", "1.0.0") is False
    assert g._is_newer("0.9.9", "1.0.0") is False
    assert g._is_newer("garbage", "1.0.0") is False


def test_pick_release_asset_per_platform():
    assets = [
        {"name": "osu-collector-gui-Setup.exe", "browser_download_url": "win"},
        {"name": "osu-collector-gui.dmg", "browser_download_url": "mac"},
        {"name": "osu-collector-gui-x86_64.AppImage", "browser_download_url": "lin"},
    ]
    assert g._pick_release_asset(assets, "win32") == "win"
    assert g._pick_release_asset(assets, "darwin") == "mac"
    assert g._pick_release_asset(assets, "linux") == "lin"
    assert g._pick_release_asset([], "win32") == ""


def test_start_rejects_when_no_ids():
    api = g.JsApi()
    res = api.start({"ids_text": "garbage", "settings": {}})
    assert res["ok"] is False
    assert "No valid" in res["error"]


def _job(**kw):
    base = dict(collection_ids=[1], output_dir=g.Path("/tmp"))
    base.update(kw)
    return g.DownloadJob(**base)


def test_merge_target_forces_osdb_generation():
    # Regression: merging into a lazer collection must generate .osdb even
    # when the user left "generate .osdb" off — otherwise _merge_into_lazer
    # silently no-ops and the chosen collection is never created.
    d = g.Downloader(_job(generate_osdb=False, add_to_lazer_collections=True),
                     lambda n, p: None)
    assert d._should_generate_osdb() is True
    assert d._should_fetch_details() is True


def test_no_merge_no_export_means_no_osdb():
    d = g.Downloader(_job(generate_osdb=False, add_to_lazer_collections=False),
                     lambda n, p: None)
    assert d._should_generate_osdb() is False


def test_explicit_export_still_generates_osdb():
    d = g.Downloader(_job(generate_osdb=True, add_to_lazer_collections=False),
                     lambda n, p: None)
    assert d._should_generate_osdb() is True
