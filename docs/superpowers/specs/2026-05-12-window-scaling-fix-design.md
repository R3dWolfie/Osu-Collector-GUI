# Window scaling fix (initial layout + Windows HiDPI)

**Date:** 2026-05-12
**Target version:** v0.6.1
**Status:** Design approved, pending implementation plan

## Motivation

Users on multiple platforms — especially Windows — report that the GUI opens with some sections missing or unreachable. Manually resizing the window brings them back. Reported as: "window scaling is completely messed up, it doesn't scale right, need to resize the window and still can't see anything sometimes, especially on Windows OS."

Symptom narrowed via clarification: it's not blurry fonts, not "too big for screen," not clipped widgets. Whole sections of the form are visually missing until the user resizes the window manually, at which point the layout snaps into place and the missing content appears. The QScrollArea wrapping the form (added in v0.5.0) was meant to be the safety net for this, but isn't doing its job at first show.

The cross-platform asymmetry — works on Linux, breaks on Windows — points at Qt's initial layout-pass timing differing between window managers, plus Windows DPI scaling magnifying small layout-calculation errors.

## Approach

Three independent, surgical changes to `osu_collector_gui.py`. Each addresses a different facet of the root cause; none of them refactors existing behavior.

1. **Set Qt's HiDPI rounding policy to PassThrough before QApplication construction.** Qt 6's default `Round` policy snaps non-integer Windows scaling (125%, 150%) to the nearest integer, producing 1-pixel-off widget heights that compound across nested QFormLayouts and trick the QScrollArea's `sizeHint()` into underestimating its content height. `PassThrough` uses the OS's exact scaling factor.

2. **Change the root scroll-content widget's vertical size policy from `Preferred` to `Minimum`.** `Preferred` lets the layout shrink the widget below its natural content height under some compositor configurations; `Minimum` forces `sizeHint()` to reflect the actual minimum needed.

3. **Override `MainWindow.showEvent` to schedule a deferred re-layout via `QTimer.singleShot(0, ...)`.** This runs after Qt's initial layout pass has settled, forcing `updateGeometry()` + `adjustSize()` on the QScrollArea's inner widget. This directly addresses the "must-resize-window-to-see-content" symptom: the user-initiated resize was triggering the same recomputation we now do automatically.

Alternative approaches considered and declined:

- **Switch to logical-units sizing.** Replace hardcoded `resize(900, 950)` / `setMinimumSize(520, 400)` with font-metric-derived values. Cleaner long-term but the hardcoded pixels haven't been a problem at integer scaling, and the HiDPI rounding policy change in (1) handles most of the same problem space at lower cost.
- **Tabbed layout (rearchitect).** Drop QScrollArea, split the form into tabs. Significant UX restructure that's worth considering on its own merits, not as a workaround for what is fundamentally a layout-calculation bug.

## Architecture

```
main()
  ↓ [NEW] setHighDpiScaleFactorRoundingPolicy(PassThrough)  ← BEFORE QApplication
  app = QApplication(sys.argv)
  win = MainWindow()
       ↓ __init__ → _build_ui
              root.setSizePolicy(Expanding, Minimum)        ← CHANGED from Preferred
              scroll.setWidget(root)
              self.setCentralWidget(scroll)
  win.show()
       ↓ triggers showEvent (NEW override)
              super().showEvent()
              if not self._initial_layout_done:
                  self._initial_layout_done = True
                  QTimer.singleShot(0, _recompute_scroll_layout)
                       ↓ (one event-loop tick later)
                       central.widget().updateGeometry()
                       central.widget().adjustSize()
```

## Components

### Changed in `osu_collector_gui.py`

| Location | Change |
|---|---|
| `from PyQt6.QtCore import ...` (line 33) | Add `QTimer` to the import list. |
| `main()` (line 2705) | Add `QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)` as the first line of `main()`, before `QApplication(sys.argv)`. |
| `MainWindow._build_ui` (line 1712) | Change `root.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)` to `root.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)`. |
| `MainWindow` (new method, place near `closeEvent` at line 2017) | Add `showEvent(self, event)` override. |
| `MainWindow` (new method, immediately after `showEvent`) | Add `_recompute_scroll_layout(self)` private method. |

### Method bodies

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

The `_initial_layout_done` guard prevents re-running the recomputation on every hide→show cycle (e.g. minimize-then-restore on Windows). The `isinstance` + `is not None` guards protect against `centralWidget()` returning unexpected values during teardown or in subclassed contexts.

## Error handling

| Failure mode | Behavior |
|---|---|
| `centralWidget()` returns None (teardown) | `isinstance` check fails, method returns silently. |
| `centralWidget()` is not a `QScrollArea` (future refactor) | Same — `isinstance` fails, method no-ops. |
| Qt timer fires after window destroyed | Qt handles by no-oping the callback; no exception. |
| HiDPI rounding policy not supported on Qt < 6.0 | Project already requires PyQt6 (see requirements.txt). Qt 6.0+ supports `HighDpiScaleFactorRoundingPolicy`. |
| User has integer scaling (100%, 200%) | `PassThrough` produces identical output to `Round` at integer factors. No behavior change. |

## Cleanup / migration

No settings keys added or removed. No CHANGELOG entry needed for this fix unless we ship it as v0.6.1; in that case a single bullet under "Fixed" suffices.

## Testing

**Unit-testable in isolation:** Pure layout behavior cannot be unit-tested without a running Qt event loop and a real display, which the test infra (added in Task 0 of v0.6.0) doesn't have. The fix is verified manually.

**Manual verification path (Linux):**

1. Launch the GUI with `python osu_collector_gui.py`. Verify the window opens at the default 900×950 size and ALL sections are visible immediately (Collections, Settings, Lazer Collections, Status). No manual resize required.
2. Click Minimize, then restore. Verify the recomputation does NOT run again (the `_initial_layout_done` guard works) — content should remain visible.
3. Resize the window to 600×500 (forcing scrollbars). Verify the scrollbar appears and reaches the bottom of the content.
4. Resize back to 900×950. Verify scrollbar disappears and all content visible.

**Manual verification path (Windows — user-driven smoke):**

5. Launch the GUI on a Windows machine at 125% or 150% display scaling.
6. Verify all sections visible on first open (this is the primary regression target).
7. Verify text and icons render crisply (the HiDPI rounding policy should also slightly improve text rendering at fractional scaling).

## Out of scope

- Logical-units sizing (font-metric-derived dimensions)
- Tabbed/split layout rearchitect
- Removing or shrinking the QScrollArea wrap

These were considered and declined as either over-scope or low-ROI relative to the targeted fix.

## Open questions resolved during brainstorming

| Question | Resolution |
|---|---|
| What does "can't see anything" actually mean? | Whole sections blank or unreachable by scrolling. |
| What does the scrollbar do? | Initial layout shows incomplete content; manual window resize fixes it. → Re-layout timing bug. |
| Pure layout fix or also HiDPI? | Both — they compound. |
| Replace QScrollArea with tabs? | No — separate UX project, not a workaround for this bug. |
| Move to logical-units sizing? | No — HiDPI rounding policy gets most of the same benefit at much lower cost. |
