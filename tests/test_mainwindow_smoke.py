"""Construct the real MainWindow headlessly to guard against GUI wiring
regressions — every attribute here is referenced by the download worker,
the settings round-trip, or a signal handler, so a rename would break the
app at runtime even though the pure-function tests still pass."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication

from osu_collector_gui import MainWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# Widgets the worker / handlers / settings persistence depend on by name.
REQUIRED_WIDGETS = [
    "ids_edit", "dir_edit", "target_combo", "refresh_collections_btn",
    "new_name_edit", "new_name_label", "download_parallel_spin",
    "import_parallel_spin", "start_btn", "cancel_btn", "export_btn",
    "status_label", "progress_bar", "log_box", "advanced_expander",
    "advanced_container", "cm_cli_edit", "realm_edit", "osu_path_edit",
    "auto_import_cb", "skip_imported_cb", "restart_lazer_cb",
    "generate_osdb_cb", "consolidate_cb", "cleanup_cb", "import_delay_spin",
    "recover_realm_btn",
]


def test_mainwindow_builds_with_all_wired_widgets(app):
    w = MainWindow()
    try:
        for attr in REQUIRED_WIDGETS:
            assert hasattr(w, attr), f"MainWindow lost widget: {attr}"
        # Settings round-trip must not raise (exercises every getter above).
        w._save_settings()
    finally:
        w.close()


def test_advanced_toggle_and_target_picker(app):
    w = MainWindow()
    try:
        w.advanced_expander.setChecked(True)
        assert "▾" in w.advanced_expander.text()
        w.advanced_expander.setChecked(False)
        assert "▸" in w.advanced_expander.text()

        # Selecting "Create new…" reveals the name field; the default hides
        # it. Drive both transitions so the handler fires regardless of any
        # persisted starting selection. isHidden() reflects the explicit
        # visibility intent without needing the window to be shown.
        default_idx = w.target_combo.findText(w.DEFAULT_TARGET)
        new_idx = w.target_combo.findText(w.NEW_TARGET)
        assert default_idx >= 0 and new_idx >= 0
        w.target_combo.setCurrentIndex(default_idx)
        assert w.new_name_edit.isHidden()
        w.target_combo.setCurrentIndex(new_idx)
        assert not w.new_name_edit.isHidden()
    finally:
        w.close()


def test_start_button_enables_with_ids(app):
    w = MainWindow()
    try:
        assert not w.start_btn.isEnabled()
        w.ids_edit.setPlainText("21362")
        assert w.start_btn.isEnabled()
    finally:
        w.close()
