# Window Scaling Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the "must-resize-window-to-see-content" symptom — especially on Windows at fractional DPI scaling — by setting Qt's HiDPI rounding policy, adjusting the scroll-content widget's size policy, and deferring a re-layout pass to the first event-loop tick after show.

**Architecture:** Three independent, surgical changes to `osu_collector_gui.py`. (1) `QApplication.setHighDpiScaleFactorRoundingPolicy(PassThrough)` in `main()` before `QApplication(sys.argv)` makes Qt honor exact OS DPI factors (1.25, 1.5) instead of rounding. (2) Root scroll-content widget vertical size policy changes from `Preferred` to `Minimum` so its `sizeHint()` reflects actual content height. (3) `MainWindow.showEvent` override schedules a `QTimer.singleShot(0, ...)` callback that calls `updateGeometry()` + `adjustSize()` on the QScrollArea's inner widget after the initial layout pass completes.

**Tech Stack:** Python 3.11+, PyQt6.

**Spec:** `docs/superpowers/specs/2026-05-12-window-scaling-fix-design.md`

---

## File Structure

All changes land in the existing single file `osu_collector_gui.py`. No new files. Plan + spec docs in `docs/superpowers/`.

| File | Change scope |
|---|---|
| `osu_collector_gui.py` | One import addition, three method/policy changes |
| `CHANGELOG.md` | Add a v0.6.1 "Fixed" entry |
| `docs/superpowers/plans/2026-05-12-window-scaling-fix.md` | This file |

---

## Task 1: Add QTimer import

**Why:** `showEvent` will use `QTimer.singleShot`. The current import line at osu_collector_gui.py:33 doesn't include `QTimer`.

**Files:**
- Modify: `osu_collector_gui.py:33`

- [ ] **Step 1: Update the QtCore import line**

In `osu_collector_gui.py`, find line 33:

```python
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
```

Change to:

```python
from PyQt6.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal
```

(Add `QTimer` alphabetically between `Qt` and `pyqtSignal`.)

- [ ] **Step 2: Verify import works**

Run:
```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python -c "from osu_collector_gui import MainWindow; from PyQt6.QtCore import QTimer; print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 3: Run all tests**

Run:
```
pytest tests/ -v
```
Expected: all 10 tests still pass.

- [ ] **Step 4: Commit**

```bash
git add osu_collector_gui.py
git commit -m "Import QTimer for upcoming showEvent fix"
```

---

## Task 2: Set HiDPI rounding policy to PassThrough

**Why:** Qt 6's default `Round` policy snaps non-integer Windows scaling (125%, 150%) to the nearest integer factor, producing 1-pixel-off widget heights. `PassThrough` uses the OS's exact DPI factor. Must be set BEFORE `QApplication(sys.argv)` to take effect.

**Files:**
- Modify: `osu_collector_gui.py:2705-2711` (`main()` function)

- [ ] **Step 1: Modify main()**

In `osu_collector_gui.py`, find the `main()` function at line 2705. It currently looks like:

```python
def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    win = MainWindow()
    win.show()
    return app.exec()
```

Change it to:

```python
def main() -> int:
    # Honor non-integer Windows DPI scaling (125%, 150%) precisely instead
    # of rounding to the nearest integer factor — Qt 6's default Round
    # policy is the root cause of 1-pixel-off widget heights that fool the
    # QScrollArea's sizeHint() into underestimating content height on
    # Windows. Must be set BEFORE constructing QApplication.
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

- [ ] **Step 2: Verify import works**

Run:
```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python -c "import osu_collector_gui; print(osu_collector_gui.main.__name__)"
```
Expected: prints `main`. (We're not actually calling `main()` — that would open a Qt window — just confirming the file parses and the function is importable.)

- [ ] **Step 3: Run all tests**

```
pytest tests/ -v
```
Expected: 10 tests pass.

- [ ] **Step 4: Commit**

```bash
git add osu_collector_gui.py
git commit -m "Set HiDPI rounding policy to PassThrough before QApplication"
```

---

## Task 3: Change root scroll-content size policy

**Why:** The root QWidget inside the QScrollArea currently uses `QSizePolicy.Policy.Preferred` for its vertical axis. `Preferred` lets the layout shrink the widget below natural content height in some compositor configurations. `Minimum` forces `sizeHint()` to reflect the actual minimum content needs, which is what QScrollArea reads to decide its viewport size and scrollbar policy.

**Files:**
- Modify: `osu_collector_gui.py:1712`

- [ ] **Step 1: Change the size policy line**

In `osu_collector_gui.py`, find line 1712 inside `MainWindow._build_ui`:

```python
        root.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
```

Change to:

```python
        root.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
```

That's the entire change — one word (`Preferred` → `Minimum`).

- [ ] **Step 2: Verify import works**

```
python -c "from osu_collector_gui import MainWindow; print('ok')"
```
Expected: prints `ok`.

- [ ] **Step 3: Run all tests**

```
pytest tests/ -v
```
Expected: 10 tests pass.

- [ ] **Step 4: Commit**

```bash
git add osu_collector_gui.py
git commit -m "Scroll content vertical size policy: Preferred -> Minimum"
```

---

## Task 4: Override showEvent + add _recompute_scroll_layout helper

**Why:** Qt's initial layout pass on first `show()` can underestimate the scroll area's content height when there are many nested QFormLayouts. Deferring a recomputation to the next event-loop tick (via `QTimer.singleShot(0, ...)`) runs it AFTER the layout has settled. Without this, users have to manually resize the window to trigger the same recomputation we're now doing automatically.

The `_initial_layout_done` flag prevents re-running on every hide→show cycle (e.g. minimize-then-restore on Windows). The `isinstance` + `is not None` guards on `centralWidget()` protect against teardown / subclass surprises.

**Files:**
- Modify: `osu_collector_gui.py` — add two methods to `MainWindow`. The natural insertion point is right after the existing `closeEvent` method (around line 2017) so the Qt event overrides live together.

- [ ] **Step 1: Locate the closeEvent method**

Run:
```
grep -n "def closeEvent" /var/home/red/Projects/Osu-Collector-GUI/osu_collector_gui.py
```
You should see a line like `2017:    def closeEvent(self, event) -> None:    # noqa: N802 (Qt override)`. Read the next 10 lines to find where that method ends (it should be a few lines — calls `_save_settings()` and `event.accept()`).

- [ ] **Step 2: Insert the two new methods immediately after closeEvent**

Add these two methods right after `closeEvent` ends, before the next method (probably an `# ----- event handlers ----------` banner comment or another method definition):

```python
    def showEvent(self, event) -> None:    # noqa: N802 (Qt override)
        super().showEvent(event)
        # Qt's initial layout pass can underestimate the scroll area's
        # content height when there are many nested QFormLayouts. Defer
        # a recomputation to the next event-loop tick so it runs AFTER
        # the layout has settled. Especially load-bearing on Windows
        # where the initial configure-notify timing differs from Linux,
        # producing the "have to resize the window to see everything"
        # symptom users hit on first open.
        if not getattr(self, "_initial_layout_done", False):
            self._initial_layout_done = True
            QTimer.singleShot(0, self._recompute_scroll_layout)

    def _recompute_scroll_layout(self) -> None:
        central = self.centralWidget()
        if isinstance(central, QScrollArea) and central.widget() is not None:
            central.widget().updateGeometry()
            central.widget().adjustSize()
```

Match the existing 4-space indentation used by other `MainWindow` methods.

- [ ] **Step 3: Verify import works**

```
python -c "from osu_collector_gui import MainWindow; mw = MainWindow.showEvent; print('showEvent:', mw.__name__); rs = MainWindow._recompute_scroll_layout; print('helper:', rs.__name__)"
```
Expected: prints `showEvent: showEvent` and `helper: _recompute_scroll_layout`. (We can't construct `MainWindow()` without a QApplication, but we can introspect the methods.)

- [ ] **Step 4: Run all tests**

```
pytest tests/ -v
```
Expected: 10 tests pass.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py
git commit -m "MainWindow: defer scroll re-layout to first event-loop tick post-show"
```

---

## Task 5: Bump version + CHANGELOG entry

**Files:**
- Modify: `osu_collector_gui.py:62` (APP_VERSION)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump APP_VERSION**

In `osu_collector_gui.py`, line 62:

```python
APP_VERSION = "0.6.1"
```

- [ ] **Step 2: Add CHANGELOG entry**

In `CHANGELOG.md`, insert a new section at the top (after the `# Changelog` header and the explanatory line, but BEFORE the existing `## [0.6.0]` section):

```markdown
## [0.6.1] — 2026-05-12

### Fixed

- **"Have to resize the window to see content" on first open.** The QScrollArea wrapping the form would underestimate its content height during Qt's initial layout pass, leaving some sections invisible until the user manually resized the window. Now `MainWindow.showEvent` schedules a deferred `updateGeometry()` + `adjustSize()` on the scroll area's inner widget via `QTimer.singleShot(0, …)`, which runs after the layout has settled. Especially load-bearing on Windows where the configure-notify timing differs from Linux.
- **Windows DPI scaling at 125% / 150%.** Qt 6's default `Round` rounding policy was snapping non-integer scaling factors to the nearest integer, producing 1-pixel-off widget heights that compounded across nested forms. Now uses `PassThrough` so Qt honors the OS's exact DPI factor. Set before `QApplication` is constructed, as required.
- **Root scroll-content vertical size policy** changed from `Preferred` to `Minimum` so its `sizeHint()` reflects actual minimum content height (matters for some compositor configurations).

```

Note: one blank line between this section and `## [0.6.0]`.

- [ ] **Step 3: Verify version**

```
python -c "import osu_collector_gui; print(osu_collector_gui.APP_VERSION)"
```
Expected: prints `0.6.1`.

- [ ] **Step 4: Run all tests**

```
pytest tests/ -v
```
Expected: 10 tests pass.

- [ ] **Step 5: Commit**

```bash
git add osu_collector_gui.py CHANGELOG.md
git commit -m "v0.6.1: window scaling fix (initial layout + HiDPI rounding)"
```

---

## Task 6: Manual smoke test (user-driven)

**Why:** Pure layout behavior cannot be unit-tested without a running Qt event loop and a real display. Verification is manual.

These are gate checks before pushing to GitHub. Do them in order. If any fails, stop and report rather than continuing.

- [ ] **Smoke 1: Linux baseline — first open**

Run:
```
cd /var/home/red/Projects/Osu-Collector-GUI
source .venv/bin/activate
python osu_collector_gui.py
```
Expected: window opens at 900×950, ALL sections visible immediately without any manual resize: Collections to download, Settings/Tuning, Lazer Collections, Status/Log. The scroll bar may or may not appear depending on your screen height — if it does, it should reach the bottom of the content.

- [ ] **Smoke 2: Linux — minimize / restore**

While the window is open, click the minimize button (or use the window manager shortcut), then click the taskbar icon / `Super+Tab` to restore.
Expected: content remains visible. The deferred re-layout does NOT run a second time (the `_initial_layout_done` guard works). No flicker or repositioning.

- [ ] **Smoke 3: Linux — resize down to minimum**

Drag the window resize handle to shrink to ~520×400 (the configured minimum).
Expected: the vertical scrollbar appears; you can scroll through all the form sections; nothing is permanently clipped.

- [ ] **Smoke 4: Linux — resize back up**

Drag the window back to ~900×950.
Expected: scrollbar disappears; all content visible again.

- [ ] **Smoke 5: Windows — first open at 125%/150% scaling (the primary regression target)**

On a Windows machine (or VM) running at 125% or 150% display scaling, launch the app (either via the .exe from the v0.6.0 CI build retagged for v0.6.1, or via Python on Windows directly).
Expected: window opens with all sections visible on first show. No manual resize required to see content. Text is crisp at the OS's actual DPI factor.

- [ ] **Smoke 6: Windows — Hide/restore + resize cycles**

Repeat Smokes 2–4 on Windows.
Expected: same behavior as Linux. The Windows-specific symptom (sections missing until manual resize) is gone.

- [ ] **All smoke tests pass — ready for review/push decision**

Once all six succeed, v0.6.1 is verified. Per existing user direction, the branch stays local until manual verification is complete.

---

## Self-Review

**Spec coverage** (vs. `docs/superpowers/specs/2026-05-12-window-scaling-fix-design.md`):

- ✅ HiDPI rounding policy = PassThrough before QApplication → Task 2
- ✅ Root scroll-content size policy `Preferred` → `Minimum` → Task 3
- ✅ MainWindow.showEvent + `_recompute_scroll_layout` with `_initial_layout_done` guard + `isinstance` / `is not None` guards → Task 4
- ✅ QTimer import → Task 1
- ✅ Version bump + CHANGELOG → Task 5
- ✅ Manual verification path (Linux + Windows) → Task 6

**Placeholder scan:** none. Every step has concrete code or exact commands with expected output.

**Type consistency:**
- `_initial_layout_done` flag spelled identically in Task 4's `showEvent` and `getattr` usage ✅
- `_recompute_scroll_layout` method name matches between definition (Task 4 Step 2) and the `singleShot` reference inside `showEvent` (same step) ✅
- `Qt.HighDpiScaleFactorRoundingPolicy.PassThrough` — confirmed correct Qt 6 enum path ✅
- `QSizePolicy.Policy.Minimum` — same enum-nested style used by the existing `Expanding` reference at the same line ✅
