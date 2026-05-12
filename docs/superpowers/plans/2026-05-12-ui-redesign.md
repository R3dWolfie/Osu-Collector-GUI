# UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dense scrolling form layout with a single-page progressive-disclosure layout in Cherry red on a dark base (#1e1e26). Drop the QScrollArea added in v0.5.0 — the simpler layout fits in a smaller window (520×680 default).

**Architecture:** Module-level `QSS` constant carries the theme; applied to `QApplication` in `main()`. `MainWindow._build_ui()` is rewritten into a single `QVBoxLayout` with rows for collection IDs, output, add-to picker, parallelism spinboxes, start/cancel, status, progress, log, and a collapsible Advanced section. Most existing widget names are preserved; vestigial widgets (download_beatmaps_cb, add_to_lazer_cb) are removed and their semantics derived from other state.

**Tech Stack:** Python 3.11+, PyQt6, Qt Style Sheets (QSS).

**Spec:** `docs/superpowers/specs/2026-05-12-ui-redesign-design.md`

---

## File Structure

All changes land in the existing single-file `osu_collector_gui.py`. No new Python files. CHANGELOG + tests update where relevant.

| File | Change scope |
|---|---|
| `osu_collector_gui.py` | Add module-level `QSS` constant; rewrite `_build_ui`; rewrite Start↔Cancel handling; drop `showEvent` + `_recompute_scroll_layout`; shrink window size; remove `download_beatmaps_cb` + `add_to_lazer_cb`; add `_update_start_enabled` helper; add `advanced_expanded` settings key |
| `tests/test_main_window.py` (NEW) | Unit tests for pure helpers (`_update_start_enabled`, target-combo defaults, settings persistence) — no Qt event-loop tests |
| `CHANGELOG.md` | v0.7.0 entry |

## Widget name reconciliation

The spec uses some clean-but-renamed widget names (`collection_ids_edit`, `output_dir_edit`). To minimize churn, **the implementation keeps the existing names** where widgets already exist. New widgets introduced by this plan get new names. Mapping:

| Spec name | Existing widget name | Status |
|---|---|---|
| `collection_ids_edit` | `self.ids_edit` (QPlainTextEdit) | KEEP — multi-line preserved |
| `output_dir_edit` | `self.dir_edit` (QLineEdit) | KEEP |
| `output_browse_btn` | (new) | NEW — small QToolButton |
| `target_combo` | `self.target_combo` | KEEP |
| `refresh_collections_btn` | `self.refresh_collections_btn` | KEEP |
| `download_parallel_spin` | `self.download_parallel_spin` | KEEP |
| `import_parallel_spin` | `self.import_parallel_spin` | KEEP |
| `import_delay_spin` | `self.import_delay_spin` | KEEP |
| `cm_cli_edit` | `self.cm_cli_edit` | KEEP |
| `cm_detect_btn` | (inline currently) | KEEP — already exists |
| `realm_edit` | `self.realm_edit` | KEEP |
| `realm_browse_btn` | (inline currently) | KEEP |
| `osu_path_edit` | `self.osu_path_edit` | KEEP |
| `osu_browse_btn` | (inline currently) | KEEP |
| `auto_import_cb` | `self.auto_import_cb` | KEEP |
| `skip_imported_cb` | `self.skip_imported_cb` | KEEP |
| `restart_lazer_cb` | `self.restart_lazer_cb` | KEEP |
| `generate_osdb_cb` | `self.generate_osdb_cb` | KEEP |
| `consolidate_cb` | `self.consolidate_cb` | KEEP |
| `cleanup_cb` | `self.cleanup_cb` | KEEP |
| `recover_realm_btn` | (inline button currently) | RENAME to this for clarity |
| `start_btn` | `self.start_btn` | KEEP |
| `cancel_btn` | `self.cancel_btn` | KEEP |
| `status_label` | (new — currently a QLabel inside log group) | NEW — promoted to a top-level row |
| `progress_bar` | `self.progress_bar` (currently exists, plus a `self.beatmap_progress_bar`) | KEEP `progress_bar`, REMOVE per-beatmap bar (rolled into log) |
| `log_box` | `self.log_box` | KEEP |
| `advanced_expander` | (new) | NEW — QToolButton with checkable + arrow indicator |
| `advanced_container` | (new) | NEW — QWidget holding all advanced rows; hidden by default |

**Removed entirely:**
- `self.download_beatmaps_cb` — always-on semantics now hardcoded to `True` in `DownloadJob.download_beatmaps`
- `self.add_to_lazer_cb` — semantics derived from `target_combo.currentText() != "Don't merge"`
- `self.beatmap_progress_bar` (per-beatmap bar) — log lines convey the same info
- `self.new_name_edit` — kept BUT only visible when the user picks "Create new collection..." from `target_combo` (existing behavior preserved)

---

## Task 1: Add the module-level QSS stylesheet + apply in main()

**Why:** Non-destructive theming step. The app still launches with the old layout, but now in Cherry red on a dark base. Verifies the stylesheet renders correctly before we touch layout.

**Files:**
- Modify: `osu_collector_gui.py` — add `QSS = """..."""` near the top (after imports + constants); modify `main()` to call `app.setStyleSheet(QSS)`

- [ ] **Step 1: Locate the right insertion point for the QSS constant**

Run:
```
grep -n "^DEFAULT_MIRROR\|^FALLBACK_MIRRORS\|^MIRROR_DEAD_TTL_S\|^APP_VERSION\|^USER_AGENT" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py | head -10
```
You should see module-level constants around lines 62–95. Insert the new `QSS` block immediately AFTER the last existing constant (likely `MIRROR_DEAD_TTL_S` or `USER_AGENT`) and BEFORE the OSDB constants / class definitions.

- [ ] **Step 2: Add the QSS constant**

In `osu_collector_gui.py`, insert this block after the existing module constants:

```python
# ---------------------------------------------------------------------------
# Theme — v0.7.0 Cherry red on dark base
# ---------------------------------------------------------------------------
#
# Applied once via QApplication.setStyleSheet in main(). Colors:
#   accent (Cherry):  #e3344f → #ffa15f gradient on progress + primary button
#   surface:          #1e1e26 (window body) / #16161c (title-bar strip)
#   fields:           #2a2a35 with #3a3a48 borders / #5a5a68 on focus
#   text:             #e8e8ec primary / #9aa0a6 muted / #7d8090 meta
#   semantic:         #5dd56e success (skipped) / #e3344f errors

QSS = """
QMainWindow, QWidget {
    background-color: #1e1e26;
    color: #e8e8ec;
    font-family: -apple-system, "Segoe UI", "Cantarell", sans-serif;
    font-size: 13px;
}

QLabel {
    color: #e8e8ec;
}
QLabel[role="micro"] {
    color: #7d8090;
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.6px;
}
QLabel[role="subgroup"] {
    color: #7d8090;
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-top: 8px;
}
QLabel[role="status"] {
    color: #7d8090;
    font-size: 11px;
}

QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {
    background-color: #2a2a35;
    border: 1px solid #3a3a48;
    border-radius: 4px;
    padding: 6px 9px;
    color: #e8e8ec;
    selection-background-color: #e3344f;
    selection-color: white;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #5a5a68;
}
QLineEdit::placeholder, QPlainTextEdit::placeholder {
    color: #5d6072;
}

QComboBox::drop-down {
    border: none;
    width: 18px;
}
QComboBox::down-arrow {
    image: none;
    border-top: 4px solid #9aa0a6;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    margin-right: 6px;
}
QComboBox QAbstractItemView {
    background-color: #2a2a35;
    border: 1px solid #3a3a48;
    color: #e8e8ec;
    selection-background-color: #e3344f;
    selection-color: white;
}

QSpinBox::up-button, QSpinBox::down-button {
    background: transparent;
    width: 14px;
    border: none;
}
QSpinBox::up-arrow {
    image: none;
    border-bottom: 4px solid #9aa0a6;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
}
QSpinBox::down-arrow {
    image: none;
    border-top: 4px solid #9aa0a6;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
}

QPushButton, QToolButton {
    background-color: #2a2a35;
    border: 1px solid #3a3a48;
    border-radius: 4px;
    padding: 7px 14px;
    color: #e8e8ec;
    font-weight: 500;
}
QPushButton:hover, QToolButton:hover {
    border-color: #5a5a68;
    background-color: #32323e;
}
QPushButton:disabled, QToolButton:disabled {
    color: #5d6072;
    background-color: #25252e;
}
QPushButton#primaryBtn {
    background-color: #e3344f;
    border: none;
    color: white;
    font-weight: 600;
    padding: 9px 18px;
}
QPushButton#primaryBtn:hover { background-color: #c92d44; }
QPushButton#primaryBtn:disabled { background-color: #4a2932; color: #8a6878; }

QCheckBox {
    color: #c0c4d0;
    spacing: 6px;
    font-size: 12px;
}
QCheckBox::indicator {
    width: 13px;
    height: 13px;
    border: 1.5px solid #5d6072;
    border-radius: 2px;
    background: #1e1e26;
}
QCheckBox::indicator:checked {
    background: #e3344f;
    border-color: #e3344f;
    image: none;
}

QProgressBar {
    background-color: #2a2a35;
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                       stop:0 #e3344f, stop:1 #ffa15f);
    border-radius: 3px;
}

QPlainTextEdit#logBox {
    background-color: #0e0e14;
    border: 1px solid #2a2a35;
    border-radius: 3px;
    padding: 8px 10px;
    color: #9aa0a6;
    font-family: "SF Mono", "Cascadia Code", "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 11px;
}

QToolButton#advancedExpander {
    background: transparent;
    border: 1px solid #2a2a35;
    color: #7d8090;
    font-size: 11px;
    padding: 7px 9px;
    text-align: left;
}
QToolButton#advancedExpander:hover {
    color: #e8e8ec;
    border-color: #3a3a48;
}
QToolButton#advancedExpander:checked {
    color: #e8e8ec;
}

QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #3a3a48;
    border-radius: 4px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover { background: #4a4a58; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
```

- [ ] **Step 3: Apply the QSS in main()**

Find `def main() -> int:` (around line 2900+ — find with `grep -n "^def main" osu_collector_gui.py`). It currently starts:

```python
def main() -> int:
    # Honor non-integer Windows DPI scaling (125%, 150%) precisely instead
    # of rounding to the nearest integer factor — Qt 6's default Round
    # ...
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    win = MainWindow()
    win.show()
    return app.exec()
```

Insert `app.setStyleSheet(QSS)` immediately after `app = QApplication(sys.argv)`:

```python
def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    win = MainWindow()
    win.show()
    return app.exec()
```

- [ ] **Step 4: Verify file imports + GUI launches**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python -c "import osu_collector_gui; print('ok')"
pytest tests/ -v
```
Expected: `ok`, all 18 tests pass.

Smoke-launch (will block — close the window manually after verifying):
```
python osu_collector_gui.py
```
Expected: window opens with the OLD layout structure (Tasks 2+ rewrite it), but now styled in dark grays with Cherry red accents. Spinboxes / buttons / checkboxes / fields all reflect the theme. Close the window.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py
git commit -m "Add QSS theme (Cherry red on dark) and apply in main()"
```

---

## Task 2: Add pure-function helpers with TDD

**Why:** Three small helpers used by the new UI behavior. All unit-testable without Qt.

**Files:**
- Create: `tests/test_main_window.py`
- Modify: `osu_collector_gui.py` — add helpers near other MainWindow helpers

- [ ] **Step 1: Write the failing tests**

Create `/var/home/red/Projects/Osu-Collector-GUI/tests/test_main_window.py`:

```python
"""Unit tests for pure-function helpers used by MainWindow.

We can't drive Qt widgets without an event loop, so these tests target
helpers that are testable in isolation: the start-enabled predicate
and the target-combo default-item logic.
"""
from osu_collector_gui import (
    should_enable_start,
    target_combo_default_label,
    target_combo_no_merge_label,
)


def test_should_enable_start_empty_text_returns_false():
    assert should_enable_start("") is False


def test_should_enable_start_whitespace_only_returns_false():
    assert should_enable_start("   \n\t  ") is False


def test_should_enable_start_with_one_id_returns_true():
    assert should_enable_start("17391") is True


def test_should_enable_start_with_multiple_ids_returns_true():
    assert should_enable_start("17391, 9, 1234") is True


def test_should_enable_start_with_multiline_returns_true():
    assert should_enable_start("17391\n9\n1234") is True


def test_target_combo_default_label_is_per_collection():
    # The default sentinel that produces v0.6.x's "one lazer collection
    # per osu!collector collection" behavior.
    assert target_combo_default_label() == "(one collection per osu!collector collection)"


def test_target_combo_no_merge_label_is_dont_merge():
    # The sentinel that disables lazer merge entirely.
    assert target_combo_no_merge_label() == "Don't merge"


def test_default_and_no_merge_labels_differ():
    # Trivially true but documents that they're separate sentinels.
    assert target_combo_default_label() != target_combo_no_merge_label()
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
pytest tests/test_main_window.py -v
```
Expected: 8 failures with `ImportError: cannot import name 'should_enable_start' from 'osu_collector_gui'`.

- [ ] **Step 3: Implement the helpers**

In `/var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py`, add these module-level functions near the other module-level helpers (just before the `class MainWindow(QMainWindow):` line — find with `grep -n "^class MainWindow"`):

```python
# ---------------------------------------------------------------------------
# UI helpers (pure functions, unit-testable without Qt)
# ---------------------------------------------------------------------------

def should_enable_start(collection_ids_text: str) -> bool:
    """The Start button is enabled iff at least one non-whitespace character
    appears in the collection-IDs field. The actual ID parsing happens at
    submit time via _parse_ids."""
    return bool(collection_ids_text.strip())


def target_combo_default_label() -> str:
    """The default sentinel item in the 'Add to' picker. Picking this
    preserves v0.6.x's behavior: one lazer collection per osu!collector
    collection, named after the collection."""
    return "(one collection per osu!collector collection)"


def target_combo_no_merge_label() -> str:
    """The sentinel item that disables lazer collection merge entirely.
    Files still download (and may still auto-import into lazer) but no
    realm modification happens."""
    return "Don't merge"
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_main_window.py -v
```
Expected: 8 passed.

Full suite:
```
pytest tests/ -v
```
Expected: 26 total (18 prior + 8 new).

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py tests/test_main_window.py
git commit -m "Add pure-function UI helpers: should_enable_start + target_combo labels"
```

---

## Task 3: Rewrite `_build_ui` to the new single-page progressive layout

**Why:** This is the heart of the redesign. Replaces ~440 lines of nested QGroupBox/QFormLayout code with a flat QVBoxLayout that follows the spec's main-view shape.

**Files:**
- Modify: `osu_collector_gui.py` — replace the entire body of `MainWindow._build_ui` (currently lines ~1842–2150)

- [ ] **Step 1: Locate the existing `_build_ui` boundary**

Run:
```
grep -n "def _build_ui\|def _load_settings" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py
```
You should see `def _build_ui(self) -> None:` start, and `def _load_settings(self) -> dict:` is the next method. The body of `_build_ui` is everything between them.

- [ ] **Step 2: Replace the entire `_build_ui` method**

Replace the entire `_build_ui` method body with the following. This is a complete rewrite — every line between `def _build_ui(self) -> None:` and the next method (`_load_settings`) gets replaced:

```python
    def _build_ui(self) -> None:
        # Single-page progressive disclosure. No QScrollArea — the layout
        # fits in a 520x680 window. Advanced section is collapsible.

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # --- Collection IDs ---
        ids_label = QLabel("Collection IDs")
        ids_label.setProperty("role", "micro")
        root.addWidget(ids_label)
        self.ids_edit = QPlainTextEdit()
        self.ids_edit.setPlaceholderText("paste osu!collector IDs…  (comma or whitespace separated)")
        self.ids_edit.setMaximumHeight(60)
        self.ids_edit.textChanged.connect(self._update_start_enabled)
        root.addWidget(self.ids_edit)

        # --- Output folder + Add to picker (two columns) ---
        two_col = QHBoxLayout()
        two_col.setSpacing(6)

        # Left col: Output folder
        out_col = QVBoxLayout()
        out_col.setSpacing(4)
        out_label = QLabel("Output")
        out_label.setProperty("role", "micro")
        out_col.addWidget(out_label)
        out_row = QHBoxLayout()
        out_row.setSpacing(0)
        self.dir_edit = QLineEdit(self.settings.get(
            "last_output_dir", str(Path.home() / "osu-collections")
        ))
        self.dir_browse_btn = QToolButton()
        self.dir_browse_btn.setText("📁")
        self.dir_browse_btn.clicked.connect(self._on_browse)
        out_row.addWidget(self.dir_edit)
        out_row.addWidget(self.dir_browse_btn)
        out_col.addLayout(out_row)
        two_col.addLayout(out_col, stretch=1)

        # Right col: Add-to picker with Refresh
        addto_col = QVBoxLayout()
        addto_col.setSpacing(4)
        addto_label = QLabel("Add to")
        addto_label.setProperty("role", "micro")
        addto_col.addWidget(addto_label)
        addto_row = QHBoxLayout()
        addto_row.setSpacing(0)
        self.target_combo = QComboBox()
        self.target_combo.setEditable(False)
        self.target_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._reset_target_combo()
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        self.refresh_collections_btn = QToolButton()
        self.refresh_collections_btn.setText("⟳")
        self.refresh_collections_btn.setToolTip(
            "Fetch existing osu!lazer collections from your client.realm "
            "via Collection Manager CLI."
        )
        self.refresh_collections_btn.clicked.connect(self._on_refresh_collections)
        addto_row.addWidget(self.target_combo, stretch=1)
        addto_row.addWidget(self.refresh_collections_btn)
        addto_col.addLayout(addto_row)
        two_col.addLayout(addto_col, stretch=1)
        root.addLayout(two_col)

        # --- New-collection-name row (only visible when "Create new..." picked) ---
        self.new_name_label = QLabel("New collection name")
        self.new_name_label.setProperty("role", "micro")
        self.new_name_label.setVisible(False)
        root.addWidget(self.new_name_label)
        self.new_name_edit = QLineEdit()
        self.new_name_edit.setPlaceholderText("Name of the new collection")
        self.new_name_edit.setText(self.settings.get("new_collection_name", ""))
        self.new_name_edit.setVisible(False)
        root.addWidget(self.new_name_edit)

        # --- Parallel downloads + Import parallelism (two columns) ---
        spin_row = QHBoxLayout()
        spin_row.setSpacing(6)

        dl_col = QVBoxLayout(); dl_col.setSpacing(4)
        dl_label = QLabel("Downloads")
        dl_label.setProperty("role", "micro")
        dl_col.addWidget(dl_label)
        self.download_parallel_spin = QSpinBox()
        self.download_parallel_spin.setRange(1, 32)
        self.download_parallel_spin.setValue(int(self.settings.get("download_parallel", 10)))
        dl_col.addWidget(self.download_parallel_spin)
        spin_row.addLayout(dl_col, stretch=1)

        im_col = QVBoxLayout(); im_col.setSpacing(4)
        im_label = QLabel("Imports")
        im_label.setProperty("role", "micro")
        im_col.addWidget(im_label)
        self.import_parallel_spin = QSpinBox()
        self.import_parallel_spin.setRange(1, 8)
        self.import_parallel_spin.setValue(int(self.settings.get("import_parallel", 1)))
        im_col.addWidget(self.import_parallel_spin)
        spin_row.addLayout(im_col, stretch=1)
        root.addLayout(spin_row)

        # --- Start / Cancel buttons (Start visible by default, Cancel hidden) ---
        self.start_btn = QPushButton("⬇  Start download")
        self.start_btn.setObjectName("primaryBtn")
        self.start_btn.clicked.connect(self._on_start)
        self.start_btn.setEnabled(False)  # enabled when ids_edit has content
        root.addWidget(self.start_btn)

        self.cancel_btn = QPushButton("✕  Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setVisible(False)
        root.addWidget(self.cancel_btn)

        # --- Status line ---
        self.status_label = QLabel("Ready")
        self.status_label.setProperty("role", "status")
        root.addWidget(self.status_label)

        # --- Progress bar (hidden when idle) ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        # --- Log box (always visible, ~110px) ---
        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(110)
        self.log_box.setMaximumHeight(180)
        self.log_box.setPlainText(
            "Ready. Paste a collection ID above and click Start to begin."
        )
        root.addWidget(self.log_box)

        # --- Advanced expander + container ---
        self.advanced_expander = QToolButton()
        self.advanced_expander.setObjectName("advancedExpander")
        self.advanced_expander.setCheckable(True)
        self.advanced_expander.setText("▸ Advanced")
        self.advanced_expander.toggled.connect(self._on_advanced_toggled)
        root.addWidget(self.advanced_expander)

        self.advanced_container = QWidget()
        self._build_advanced(self.advanced_container)
        self.advanced_container.setVisible(False)
        root.addWidget(self.advanced_container)

        # Restore advanced-expanded state from settings.
        if self.settings.get("advanced_expanded", False):
            self.advanced_expander.setChecked(True)
            self.advanced_expander.setText("▾ Advanced")
            self.advanced_container.setVisible(True)

        # Initial Start-button state based on whatever the ids_edit holds.
        self._update_start_enabled()
```

- [ ] **Step 3: Add the `_build_advanced` helper method**

Immediately after `_build_ui` (and before `_load_settings`), add a new method that constructs the Advanced section content:

```python
    def _build_advanced(self, parent: QWidget) -> None:
        """Build the contents of the collapsible Advanced section."""
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        # ---- Paths subgroup ----
        paths_label = QLabel("Paths")
        paths_label.setProperty("role", "subgroup")
        layout.addWidget(paths_label)

        layout.addWidget(self._small_label("CM CLI command"))
        cm_row = QHBoxLayout(); cm_row.setSpacing(0)
        _saved_cmd = self.settings.get("cm_cli_command", [])
        _saved_cmd_text = shlex.join(_saved_cmd) if isinstance(_saved_cmd, list) and _saved_cmd else ""
        self.cm_cli_edit = QLineEdit(_saved_cmd_text)
        self.cm_cli_edit.setPlaceholderText("(auto-detect: wine flatpak or native CM CLI)")
        self.cm_cli_edit.textChanged.connect(self._update_skip_imported_enabled)
        cm_detect = QToolButton()
        cm_detect.setText("Auto-detect")
        cm_detect.clicked.connect(self._on_detect_cm)
        cm_row.addWidget(self.cm_cli_edit)
        cm_row.addWidget(cm_detect)
        layout.addLayout(cm_row)

        layout.addWidget(self._small_label("client.realm"))
        realm_row = QHBoxLayout(); realm_row.setSpacing(0)
        self.realm_edit = QLineEdit(self.settings.get(
            "lazer_realm_path", str(_default_lazer_realm_path())
        ))
        realm_browse = QToolButton()
        realm_browse.setText("📁")
        realm_browse.clicked.connect(self._on_browse_realm)
        realm_row.addWidget(self.realm_edit)
        realm_row.addWidget(realm_browse)
        layout.addLayout(realm_row)

        layout.addWidget(self._small_label("osu!lazer binary"))
        osu_row = QHBoxLayout(); osu_row.setSpacing(0)
        self.osu_path_edit = QLineEdit(self.settings.get("osu_binary", ""))
        self.osu_path_edit.setPlaceholderText("(auto-detect)")
        osu_browse = QToolButton()
        osu_browse.setText("📁")
        osu_browse.clicked.connect(self._on_browse_osu)
        osu_row.addWidget(self.osu_path_edit)
        osu_row.addWidget(osu_browse)
        layout.addLayout(osu_row)

        # ---- Behavior subgroup ----
        beh_label = QLabel("Behavior")
        beh_label.setProperty("role", "subgroup")
        layout.addWidget(beh_label)

        self.auto_import_cb = QCheckBox("Auto-import maps into osu!lazer")
        self.auto_import_cb.setChecked(bool(self.settings.get("auto_import", True)))
        layout.addWidget(self.auto_import_cb)

        self.skip_imported_cb = QCheckBox("Skip beatmapsets already imported")
        self.skip_imported_cb.setChecked(bool(self.settings.get("skip_already_imported", True)))
        layout.addWidget(self.skip_imported_cb)

        self.restart_lazer_cb = QCheckBox("Restart osu!lazer after merging")
        self.restart_lazer_cb.setChecked(bool(self.settings.get("restart_lazer_after", True)))
        layout.addWidget(self.restart_lazer_cb)

        self.generate_osdb_cb = QCheckBox("Generate .osdb files (export-only)")
        self.generate_osdb_cb.setChecked(bool(self.settings.get("generate_osdb", False)))
        layout.addWidget(self.generate_osdb_cb)

        self.consolidate_cb = QCheckBox("Consolidate .osdb into db/ subfolder")
        self.consolidate_cb.setChecked(bool(self.settings.get("consolidate_osdb", False)))
        layout.addWidget(self.consolidate_cb)

        self.cleanup_cb = QCheckBox("Cleanup folders after import")
        self.cleanup_cb.setChecked(bool(self.settings.get("cleanup_after_import", False)))
        layout.addWidget(self.cleanup_cb)

        # ---- Tuning subgroup ----
        tun_label = QLabel("Tuning")
        tun_label.setProperty("role", "subgroup")
        layout.addWidget(tun_label)

        layout.addWidget(self._small_label("Import delay"))
        self.import_delay_spin = QSpinBox()
        self.import_delay_spin.setRange(0, 5000)
        self.import_delay_spin.setSuffix(" ms")
        self.import_delay_spin.setSingleStep(50)
        self.import_delay_spin.setValue(int(self.settings.get("import_delay_ms", 300)))
        layout.addWidget(self.import_delay_spin)

        # ---- Maintenance subgroup ----
        maint_label = QLabel("Maintenance")
        maint_label.setProperty("role", "subgroup")
        layout.addWidget(maint_label)

        self.recover_realm_btn = QPushButton("Recover realm from backup…")
        self.recover_realm_btn.clicked.connect(self._on_recover_realm)
        layout.addWidget(self.recover_realm_btn)

        # Keep the skip-imported gating logic alive even though the checkbox
        # is now buried in Advanced (still needs CM CLI to be configured).
        self._update_skip_imported_enabled()

    @staticmethod
    def _small_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("role", "micro")
        return lbl
```

- [ ] **Step 4: Add the new slot methods**

Still in `MainWindow`, just after the existing `closeEvent` (around what is currently line ~2194), add the three new slots:

```python
    def _update_start_enabled(self) -> None:
        self.start_btn.setEnabled(should_enable_start(self.ids_edit.toPlainText()))

    def _on_advanced_toggled(self, checked: bool) -> None:
        self.advanced_container.setVisible(checked)
        self.advanced_expander.setText("▾ Advanced" if checked else "▸ Advanced")
```

- [ ] **Step 5: Add QToolButton + QProgressBar to the PyQt6.QtWidgets imports**

Find the imports near the top of the file:
```
grep -n "from PyQt6.QtWidgets import" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py
```
The line is around 35–55 and is multi-line. Make sure it includes `QToolButton`, `QProgressBar`, and `QPlainTextEdit` (the latter may already be there). If any are missing, add them alphabetically.

- [ ] **Step 6: Verify imports + smoke-launch**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python -c "from osu_collector_gui import MainWindow; print('ok')"
pytest tests/ -v
```
Expected: `ok`, 26 tests pass.

Smoke launch:
```
python osu_collector_gui.py
```
Expected: a 520×680 window with the new layout. All the elements from the mockup should be present: Collection IDs (multi-line input), Output folder + Add-to picker (two columns), Downloads + Imports spinboxes (two columns), red Start button (disabled until you type IDs), Status line, Log box, Advanced expander. Close the window.

- [ ] **Step 7: Commit**

```bash
git add osu_collector_gui.py
git commit -m "Rewrite _build_ui to single-page progressive layout (Cherry theme)"
```

---

## Task 4: Drop showEvent + _recompute_scroll_layout (v0.6.1 dead code)

**Why:** The QScrollArea is gone, so the v0.6.1 scroll-recomputation machinery is no longer needed. Deleting it removes 18 lines of code that exist only to fix a bug that no longer exists.

**Files:**
- Modify: `osu_collector_gui.py` — delete `showEvent` + `_recompute_scroll_layout` + the `_initial_layout_done` flag

- [ ] **Step 1: Locate the methods**

```
grep -n "def showEvent\|def _recompute_scroll_layout\|_initial_layout_done" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py
```
You should see `def showEvent` and `def _recompute_scroll_layout` inside the `MainWindow` class.

- [ ] **Step 2: Delete both methods**

Delete the entire `showEvent` method (the one with the `# noqa: N802 (Qt override)` comment that schedules `QTimer.singleShot(0, self._recompute_scroll_layout)`) and the entire `_recompute_scroll_layout` method. Also remove the `_initial_layout_done` attribute use if it appears anywhere else (grep should show it's only inside the methods being deleted).

The methods to delete look approximately like:

```python
    def showEvent(self, event) -> None:    # noqa: N802 (Qt override)
        super().showEvent(event)
        if not getattr(self, "_initial_layout_done", False):
            self._initial_layout_done = True
            QTimer.singleShot(0, self._recompute_scroll_layout)

    def _recompute_scroll_layout(self) -> None:
        central = self.centralWidget()
        if isinstance(central, QScrollArea) and central.widget() is not None:
            central.widget().updateGeometry()
            central.widget().adjustSize()
```

- [ ] **Step 3: Remove the now-unused QTimer import (optional but clean)**

Check if anything else uses `QTimer`:
```
grep -n "QTimer" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py
```
If only the import line remains, remove `QTimer` from the `from PyQt6.QtCore import` line. (If it's still used elsewhere, leave it.)

- [ ] **Step 4: Run tests + smoke-launch**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
pytest tests/ -v
```
Expected: 26 tests pass.

```
python osu_collector_gui.py
```
Expected: window opens normally — no scroll area means the showEvent fix is irrelevant. Close the window.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py
git commit -m "Drop showEvent + _recompute_scroll_layout (v0.6.1 dead code, no scroll area)"
```

---

## Task 5: Shrink default window size + remove min-size dependency

**Why:** The simpler layout fits in a 520×680 window. The current 900×950 default is now wasteful.

**Files:**
- Modify: `osu_collector_gui.py` — `MainWindow.__init__`

- [ ] **Step 1: Locate the resize() and setMinimumSize() calls**

```
grep -n "self.resize\|self.setMinimumSize\|setWindowTitle" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py | head -5
```
They should be in `MainWindow.__init__`, near the top.

- [ ] **Step 2: Change the values**

Find the lines (current shape):
```python
        self.resize(900, 950)
        self.setMinimumSize(520, 400)
```

Change to:
```python
        self.resize(520, 680)
        self.setMinimumSize(480, 500)
```

- [ ] **Step 3: Verify + smoke-launch**

```
pytest tests/ -v
python osu_collector_gui.py
```
Expected: tests pass; window opens at 520×680 and can be resized down to 480×500 minimum.

- [ ] **Step 4: Commit**

```bash
git add osu_collector_gui.py
git commit -m "Shrink default window 900x950 -> 520x680 (simpler layout fits)"
```

---

## Task 6: Remove vestigial widgets + derive their semantics

**Why:** `download_beatmaps_cb` and `add_to_lazer_cb` no longer exist in the UI. Their previous boolean values must be derived from the new UI state so `_on_start` still constructs a correct `DownloadJob`.

**Files:**
- Modify: `osu_collector_gui.py` — `_on_start` (computes the booleans differently); `_save_settings` (drops the dead keys)

- [ ] **Step 1: Locate `_on_start`**

```
grep -n "def _on_start" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py
```
The method currently reads from `self.download_beatmaps_cb.isChecked()` and `self.add_to_lazer_cb.isChecked()`.

- [ ] **Step 2: Update the boolean derivation in `_on_start`**

Find these two lines (or similar) inside `_on_start`:

```python
        add_to_lazer = self.add_to_lazer_cb.isChecked()
        # ...
            download_beatmaps=self.download_beatmaps_cb.isChecked(),
```

Replace with:

```python
        # add_to_lazer is now derived from the picker — "Don't merge"
        # disables merge; everything else enables it.
        add_to_lazer = self.target_combo.currentText() != target_combo_no_merge_label()
```

And in the `DownloadJob(...)` constructor call, change:
```python
            download_beatmaps=self.download_beatmaps_cb.isChecked(),
```
to:
```python
            download_beatmaps=True,
```

- [ ] **Step 3: Update `_save_settings` to stop persisting dead keys**

Find `_save_settings`:
```
grep -n "def _save_settings" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py
```

In its `json.dumps({...})` dict literal, find and DELETE these two lines:
```python
            "download_beatmaps": self.download_beatmaps_cb.isChecked(),
            "add_to_lazer_collections": self.add_to_lazer_cb.isChecked(),
```

Add this line (anywhere in the dict, but `advanced_expanded` is the new key for Task 7 — for now we'll add it later; just ensure the two dead lines are gone):

- [ ] **Step 4: Run tests + smoke**

```
pytest tests/ -v
python osu_collector_gui.py
```
Expected: tests pass; GUI opens, the picker defaults to "(one collection per osu!collector collection)", Start button works.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py
git commit -m "Drop download_beatmaps_cb + add_to_lazer_cb; derive from new UI state"
```

---

## Task 7: Add advanced_expanded settings persistence

**Why:** Remembers whether the user had Advanced expanded last time, so the GUI restores that state.

**Files:**
- Modify: `osu_collector_gui.py` — `_save_settings`

- [ ] **Step 1: Add advanced_expanded to the save dict**

In `_save_settings`, the `json.dumps({...})` dict — add this new key (anywhere in the dict):

```python
            "advanced_expanded": self.advanced_expander.isChecked(),
```

(The load side is already covered by Task 3's `_build_ui` — it reads `self.settings.get("advanced_expanded", False)` after building the expander.)

- [ ] **Step 2: Verify**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python osu_collector_gui.py
```
Smoke: open Advanced, close the window, relaunch — Advanced should be expanded on relaunch. Close again, leave collapsed, relaunch — Advanced should be collapsed. Close.

```
pytest tests/ -v
```
Expected: 26 pass.

- [ ] **Step 3: Commit**

```bash
git add osu_collector_gui.py
git commit -m "Persist advanced_expanded across launches"
```

---

## Task 8: Version bump + CHANGELOG

**Files:**
- Modify: `osu_collector_gui.py:62` (`APP_VERSION`)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump APP_VERSION**

In `osu_collector_gui.py`, line 62 currently reads `APP_VERSION = "0.6.2"`. Change to:

```python
APP_VERSION = "0.7.0"
```

- [ ] **Step 2: Add CHANGELOG entry**

In `/var/home/red/Projects/Osu-Collector-GUI/CHANGELOG.md`, insert a new section at the top — after the `# Changelog` header + blurb, BEFORE the existing `## [0.6.2]` section:

```markdown
## [0.7.0] — 2026-05-12

The "actually looks good now" release. Replaces the dense scrolling QFormLayout stack from v0.5.0 with a single-page progressive-disclosure layout themed in Cherry red on a dark base. Functional behavior (download, probe, merge, mirrors) is unchanged — this is purely structure + styling.

### Changed

- **Layout** — single-page progressive disclosure. Main view shows only the essentials (collection IDs, output, add-to picker, two parallelism spinboxes, Start, status, progress, log). Everything else (paths, behavior toggles, import delay, realm-recovery) lives behind a collapsible "Advanced" expander that's closed by default.
- **Theme** — module-level QSS applied to QApplication. Cherry red accent (#e3344f → #ffa15f gradient on Start button and progress bar) on a #1e1e26 surface. Custom-styled QSpinBox arrows, QCheckBox indicators, scrollbars, and dropdowns. Title bar reads "osu-collector-gui by Red".
- **Default window size** 900×950 → 520×680 (480×500 min). The QScrollArea wrap from v0.5.0 is gone — the layout fits.
- **Default picker** is "(one collection per osu!collector collection)" — preserves v0.6.x merge-by-default. "Don't merge" is still an option.
- **Start button** disabled until at least one non-whitespace character appears in the Collection IDs field. Replaced with a neutral-styled Cancel button during a run.
- **Log box** always visible (~110px, monospace 11px) with idle placeholder "Ready. Paste a collection ID above and click Start to begin."

### Removed

- **"Download beatmaps" toggle** — always on. Disabling it disabled the core feature, so it was dead UI.
- **"Add downloaded maps to osu!lazer collections" master toggle** — subsumed by the "Don't merge" option in the Add-to picker.
- **Per-beatmap progress bar** — redundant with the per-line log output.
- **`showEvent` / `_recompute_scroll_layout` machinery** from v0.6.1 — no scroll area means no scroll-recomputation needed.

### Other

- New `advanced_expanded` settings key persists whether the Advanced section was open at last close. New users default to collapsed.
- New `tests/test_main_window.py` with 8 unit tests for the pure-function UI helpers (`should_enable_start`, target-combo sentinel labels).

```

Note: one blank line between this section and `## [0.6.2]`.

- [ ] **Step 3: Verify version**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python -c "import osu_collector_gui; print(osu_collector_gui.APP_VERSION)"
```
Expected: `0.7.0`.

- [ ] **Step 4: Run tests**

```
pytest tests/ -v
```
Expected: 26 green.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py CHANGELOG.md
git commit -m "v0.7.0: single-page Cherry-red UI redesign"
```

---

## Task 9: Manual smoke tests (user-driven)

**Why:** Visual + layout + cross-platform behavior cannot be unit-tested. These are gate checks before pushing.

- [ ] **Smoke 1: First open**

```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python osu_collector_gui.py
```
Expected:
- Window at 520×680
- Dark gray base, Cherry red Start button (disabled — grayed)
- Collection IDs field shows placeholder "paste osu!collector IDs…"
- Output folder pre-filled from settings, Add-to picker shows "(one collection per osu!collector collection)"
- Downloads = 10, Imports = 7 (or whatever your settings persisted)
- Status reads "Ready"
- Log box shows idle placeholder
- Advanced collapsed

- [ ] **Smoke 2: Start enables on typing**

Type "17391" into the Collection IDs field. Start button enables (Cherry red brightens). Clear the field. Start disables. Type whitespace only. Start stays disabled. Type "17391" again. Start enables.

- [ ] **Smoke 3: Advanced toggles + persists**

Click "▸ Advanced". The container expands showing Paths / Behavior / Tuning / Maintenance subgroups with all the v0.6.x options visible. Click "▾ Advanced" again — collapses. Close the window with Advanced expanded. Relaunch. Confirm Advanced is expanded on open. Close with it collapsed. Relaunch. Confirm it's collapsed.

- [ ] **Smoke 4: Run a small collection**

With Advanced collapsed and the picker on its default, paste a small real collection ID (find one at https://osucollector.com — anything under ~50 maps for the smoke). Click Start.
Expected:
- Start button swaps to neutral-gray Cancel
- Progress bar fades in showing Cherry → orange gradient fill
- Status line shows live counts ("X / Y · Z skipped · M MB/s" or just "X / Y" if not merging)
- Log box clears the placeholder and shows per-set lines as they download
- All previous v0.6.x behaviors work (probe runs if skip is on, merge runs at the end if a non-"Don't merge" target was picked, lazer restarts if that's enabled)
- On completion: Cancel reverts to Start, status shows final summary, progress bar fades after a few seconds

- [ ] **Smoke 5: Cancel mid-run**

Repeat Smoke 4 but click Cancel mid-batch. The worker should stop cleanly. Cancel reverts to Start (re-enabled because IDs still in the field). Status shows "Cancelled". No realm modifications happened.

- [ ] **Smoke 6: "Don't merge" path**

Set the picker to "Don't merge". Start a small collection. Confirm: maps download, status doesn't show "skipped" counts (no probe), no lazer-merge happens, log doesn't show `[probe]` or `[lazer]` lines.

- [ ] **Smoke 7: Window resize floor**

Drag the window down to 480×500 (minimum). Confirm: everything still readable; no widget gets clipped off the bottom; log box shrinks but stays usable.

- [ ] **Smoke 8 (Windows-only, user has Windows access)**

Launch on Windows at 125% / 150% display scaling. Confirm the theme renders crisply (Qt's HiDPI rounding policy from v0.6.1 carries over), all sections fit in the default window, no clipping. The native Windows title bar coexists fine with the dark body styling.

- [ ] **All smokes pass — v0.7.0 ready for review/push decision**

---

## Self-Review

**Spec coverage** (vs. `docs/superpowers/specs/2026-05-12-ui-redesign-design.md`):

- ✅ Module-level `QSS` constant + applied in `main()` → Task 1
- ✅ `should_enable_start` pure helper → Task 2
- ✅ Target combo default sentinel labels → Task 2
- ✅ Rewritten `_build_ui` with single-page progressive layout → Task 3
- ✅ Collection IDs (multi-line QPlainTextEdit, ~60px max) → Task 3
- ✅ Output + Add-to two-column row → Task 3
- ✅ Refresh button on Add-to picker → Task 3
- ✅ Downloads + Imports two-column spinbox row → Task 3
- ✅ Primary Start button with `objectName="primaryBtn"` for QSS → Task 3
- ✅ Cancel button (initially hidden, swaps with Start during run) → Task 3
- ✅ Status label + progress bar + log box (with idle placeholder) → Task 3
- ✅ Advanced expander with checkable toggle + container → Task 3
- ✅ Advanced subgroups: Paths / Behavior / Tuning / Maintenance → Task 3 (via `_build_advanced`)
- ✅ `auto_import_cb`, `skip_imported_cb`, `restart_lazer_cb`, `generate_osdb_cb`, `consolidate_cb`, `cleanup_cb` all preserved in Behavior → Task 3
- ✅ Import delay in Tuning, recover-realm in Maintenance → Task 3
- ✅ Drop showEvent + _recompute_scroll_layout → Task 4
- ✅ Window size 520×680 / min 480×500 → Task 5
- ✅ Drop `download_beatmaps_cb` + `add_to_lazer_cb` → Task 6
- ✅ Derive `add_to_lazer` from picker, `download_beatmaps=True` → Task 6
- ✅ `advanced_expanded` settings persistence → Task 7
- ✅ Version + CHANGELOG → Task 8
- ✅ Manual smoke tests → Task 9

**Placeholder scan:** none — every step has actual code or actual commands with expected output.

**Type consistency:**
- `should_enable_start` signature matches between definition (Task 2) and call site (`_update_start_enabled` in Task 3) ✅
- `target_combo_default_label` / `target_combo_no_merge_label` used in Task 6 match their Task 2 definitions ✅
- All preserved widget names (`ids_edit`, `dir_edit`, `target_combo`, etc.) match between Task 3's rewritten `_build_ui` and existing slot methods (`_on_start`, `_on_browse`, `_on_target_changed`, etc.) ✅
- `advanced_expander` / `advanced_container` introduced in Task 3 used consistently in Task 7's settings persistence ✅
- `objectName="primaryBtn"` and `objectName="logBox"` match the QSS selectors in Task 1's stylesheet ✅
- `role` property values (`"micro"`, `"subgroup"`, `"status"`) match between Task 3 widget creation and Task 1 QSS selectors ✅
