# UI redesign — single-page progressive, Cherry-red dark theme

**Date:** 2026-05-12
**Target version:** v0.7.0
**Status:** Design approved, pending implementation plan

## Motivation

User feedback after v0.6.x work: **"the UI already looks way too much."** Specifically:

- **Too dense** — every setting/option/path/spinbox lives on a single scrolling page. The Lazer Collections section alone shows 8+ fields before the user is sure they care about lazer integration.
- **Looks dated / utilitarian** — default Qt look, no personality, no visual hierarchy. Form labels + group boxes + tooltips per row produce a wall of chrome.

The current layout (a single scrolling QFormLayout-stack wrapped in a QScrollArea, added in v0.5.0 and patched again in v0.6.1) carries every option visible by default. New v0.6.0 features (skip-imported checkbox, parallel-downloads spinbox) made this worse by adding more rows. Without a structural redesign, every future feature compounds the density problem.

This spec applies a **single-page progressive-disclosure layout** with a deliberate visual style. Goals:

1. **First-run user pastes a collection ID, clicks Start, and goes.** Everything else is either reasonable-default or one expansion click away.
2. **The window looks like a tool that belongs alongside osu!lazer** rather than a generic Qt form.
3. **No scope creep** — the redesign covers structure and styling only. Functional behavior (download, probe, merge, mirrors) is unchanged.

## Approach

**Structure (chosen via mockup comparison):** single-page progressive — the whole app stays on one screen, essentials are visible at the top, secondary settings live inside a collapsible "Advanced" section that's closed by default. Rejected alternatives:

- *Top tabs* — works but adds navigation cost for a tool the user often opens just to paste an ID. The user picked single-page.
- *Left sidebar nav* — costs ~130 px of horizontal space and signals "settings-heavy app." Wrong feel for a focused tool.

**Visual theme (chosen via mockup comparison):** Cherry red on a near-black base.

- Primary accent: **#e3344f → #ffa15f gradient** (Cherry). Used for the primary Start button and progress fill.
- Surface: **#1e1e26** (window body) / **#16161c** (title-bar strip).
- Field surfaces: **#2a2a35** with **#3a3a48** borders.
- Text: **#e8e8ec** (primary) / **#9aa0a6** (muted) / **#7d8090** (very muted/meta) / **#5dd56e** (success/skipped) / **#e3344f** (errors).
- Branding: title bar reads `osu-collector-gui by Red v0.7.0` (matches existing `APP_AUTHOR = "Red"`).

Light mode + theme toggle are **out of scope** for v0.7.0. Single dark theme keeps the spec contained; light mode can be added later if requested.

## Architecture

```
MainWindow (QMainWindow, ~520×680 default, ~480×500 min)
  └── central widget (QWidget) ← NOT wrapped in QScrollArea anymore (window
                                  fits its content; no scrolling needed)
       └── QVBoxLayout (root, 14px margin, 10px spacing)
            ├── Collection IDs row              ← input field
            ├── Output + Add-to row             ← two-column
            ├── Parallel-downloads + Imports    ← two-column spinboxes
            ├── Start/Cancel button             ← primary action
            ├── Status line                     ← text status
            ├── Progress bar                    ← visible during runs
            ├── Log strip                       ← always visible, ~110px
            └── Advanced expander               ← collapsible
                  └── (when expanded) Paths / Behavior / Tuning / Maintenance
```

Application-wide styling is delivered via a single QSS (Qt Style Sheet) string set on `QApplication` in `main()`. The QSS string lives at module level for readability and is documented in-line.

## Components

### Top-of-window controls (always visible)

| Widget | Purpose | Default | Notes |
|---|---|---|---|
| `collection_ids_edit: QLineEdit` | osu!collector collection IDs (comma/whitespace separated) | empty, placeholder `"paste osu!collector IDs…"` | Triggers `_update_start_enabled` on `textChanged` |
| `output_dir_edit: QLineEdit` + `output_browse_btn: QToolButton` | Output folder path | from settings, fallback `~/osu-collections` | Browse button shows a small folder icon |
| `target_combo: QComboBox` + `refresh_collections_btn: QToolButton` | Add-to-lazer picker (existing lazer collections + sentinels) | **"(one collection per osu!collector collection)"** ← the existing default; previously this was the picker's first item | Refresh button shows ⟳ glyph; populates via CM CLI on click |
| `download_parallel_spin: QSpinBox` | Concurrent .osz downloads | 10 (1..32) | Existing widget from v0.6.0 |
| `import_parallel_spin: QSpinBox` | Concurrent imports into osu!lazer | 1..8, default 7 | Existing widget |
| `start_btn: QPushButton` | Start the batch | enabled only when `collection_ids_edit.text().strip()` is non-empty | Primary style (Cherry red). Replaced with `cancel_btn` during a run. |
| `status_label: QLabel` | One-line status — "Ready", "8,432 / 11,034 · 2,602 skipped · 2.1 MB/s", or error text | "Ready" when idle | Numbers white, "skipped" green, error red |
| `progress_bar: QProgressBar` | Cherry → orange gradient fill | hidden when idle | Style sheet applies the gradient via `::chunk` |
| `log_box: QPlainTextEdit` | Live log, monospace, ~110px tall, scrollable | Idle placeholder: `"Ready. Paste a collection ID above and click Start to begin."` | Read-only, word-wrap off, copy/select allowed |

### Advanced section (collapsed by default)

A single `Advanced` header row with a triangle indicator (▸ collapsed, ▾ expanded). Clicking toggles visibility of the child group. The group itself contains four labeled subgroups, each rendered as a small label + the relevant widgets:

**Paths:**
- `cm_cli_edit: QLineEdit` (CM CLI command, shown as quoted argv-list)
- `cm_detect_btn: QPushButton` ("Auto-detect")
- `realm_edit: QLineEdit` (client.realm path)
- `realm_browse_btn: QToolButton`
- `osu_path_edit: QLineEdit` (osu!lazer binary, with `(auto-detect)` placeholder)
- `osu_browse_btn: QToolButton`

**Behavior** (all `QCheckBox`):
- `auto_import_cb` — "Auto-import maps into osu!lazer" (default ON)
- `skip_imported_cb` — "Skip beatmapsets already imported" (default ON — v0.6.0 default)
- `restart_lazer_cb` — "Restart osu!lazer after merging" (default ON)
- `generate_osdb_cb` — "Generate .osdb files (export-only)" (default OFF)
- `consolidate_cb` — "Consolidate .osdb into db/ subfolder" (default OFF)
- `cleanup_cb` — "Cleanup folders after import" (default OFF)

**Tuning:**
- `import_delay_spin: QSpinBox` ("Import delay", suffix " ms", 0–5000, default 300)

**Maintenance:**
- `recover_realm_btn: QPushButton` — "Recover realm from backup…"

### Removed from UI entirely

Two existing options become implicit and don't appear anywhere:

- **"Download beatmaps" toggle** — always on. Turning it off used to disable the core feature; removing it just drops dead UI.
- **"Add downloaded maps to osu!lazer collections" master toggle** — subsumed by the `target_combo`'s "Don't merge" option. Picking "Don't merge" means no realm merge; picking anything else means merge. The boolean is computed at job-construction time.

## State transitions

| Trigger | Effect |
|---|---|
| Window opens | `start_btn` disabled (no collection IDs), `progress_bar` hidden, `log_box` shows idle placeholder, Advanced collapsed |
| User types into `collection_ids_edit` | If text is non-empty: `start_btn` enabled |
| User clicks `start_btn` | `start_btn` swapped for `cancel_btn` (same position, neutral gray styling), `progress_bar` shown, status line shows live counts, log_box clears the placeholder and starts emitting per-line output |
| Worker emits per-set lines | Each line appended to `log_box`; auto-scroll to bottom unless user has manually scrolled up |
| Worker finishes (batch_finished) | `cancel_btn` swapped back to `start_btn`, status line shows final summary, progress_bar stays at 100% briefly (3s) then hides |
| User clicks `cancel_btn` | Worker cancellation triggered (existing behavior), button stays as "Cancelling…" disabled until worker actually exits |
| User clicks Advanced header | Toggle child group visibility; persist `advanced_expanded` to settings so the state is restored next launch |

## Visual style — concrete details

**Window size.** Default `(520, 680)`. Minimum `(480, 500)`. No more 900×950 or QScrollArea — the simpler layout fits in a smaller box.

**Font.** Qt default per platform (no bundled fonts). Sizes:
- Body / fields: 13px (Qt's `QApplication.font()` baseline + 0)
- Labels (UPPERCASE): 9px, letter-spacing 0.6px, color `#7d8090`, font-weight 600
- Title bar app name: 11px, weight 500
- Log box: monospace at 11px (system mono — `SF Mono` / `Cascadia Code` / `Consolas` / fallback `monospace`)

**Spacing.** Root layout: 14px margins, 10px vertical spacing between rows. Two-column rows: 6px gap. Buttons: 8px vertical / 14px horizontal padding.

**Border radius.** All fields/buttons: 4px. Window itself: native OS chrome (we don't custom-draw the title bar).

**Hover/focus.** Fields gain a brighter border (#5a5a68) on focus. Primary button darkens slightly on hover (#c92d44).

**Progress bar.** Cherry-to-orange gradient (`linear-gradient(90deg, #e3344f, #ffa15f)`) on the `::chunk` selector; track is `#2a2a35`. Height 5px (slim).

**Log box styling.** Background `#0e0e14` (darker than window — visually nested), 1px border `#2a2a35`, padding 8px/10px. ANSI-style colored prefixes via `QPlainTextEdit.appendHtml`:
- `[N/total] filename.osz` — green `[N/total]` then default-color filename
- `[probe] …` — Cherry-red bracket, default body
- `[skip …]` — muted gray
- `[error …]` — red

## QSS architecture

A single module-level constant `QSS = """..."""` containing the full stylesheet. Applied in `main()` via `app.setStyleSheet(QSS)`. Selectors are class-based wherever possible (`QPushButton#primaryBtn`, `QLineEdit`, `QGroupBox#advancedGroup`) so the styling is co-located in one block and decoupled from widget assembly logic.

This is the **single biggest implementation lever**: most of the visual change is QSS, not Python. The widget tree is similar to today's; what changes is how widgets are styled and how they're laid out (single QVBoxLayout instead of nested QGroupBox + QFormLayouts).

QSS is bundled into the file as a triple-quoted string. No external `.qss` file. Trade-off: easier to ship (single-file project convention), slightly harder to edit (no syntax highlighting in Python literals). The QSS is short enough (~150 lines) that this is acceptable.

## Settings persistence

| Key | Default | Notes |
|---|---|---|
| All existing keys (output_dir, cm_cli_command, lazer_realm_path, etc.) | unchanged | Existing semantics preserved |
| `advanced_expanded: bool` | `false` | New — remembers if user expanded the Advanced section |

The "Download beatmaps" and "Add downloaded maps to osu!lazer collections" keys still get written to `settings.json` for backward-compat reads from older versions, but their values are derived rather than persisted from a widget. We can drop these keys entirely in v0.8.0 after one release cycle.

## Implementation strategy

Implementation lands as a substantial rewrite of `_build_ui` and the addition of a module-level `QSS` constant. The slot connections (`_on_start`, `_on_browse`, etc.) are **mostly preserved as-is** — the widgets they reference change names but the logic doesn't. A renaming sweep + a few signal-rewiring lines covers the majority of slot changes.

The implementation plan should sequence:

1. **Add the QSS constant + apply it in main()** — non-destructive; the old layout still works but with new colors. Verify the app still launches and is somewhat usable.
2. **Rewrite `_build_ui`** to the new layout structure. Reuse existing slot methods.
3. **Wire new state-transition behaviors** (Start↔Cancel swap, disabled-until-IDs, Advanced expanded persistence, idle log placeholder).
4. **Drop the QScrollArea** and the v0.6.1 showEvent / `_recompute_scroll_layout` machinery (no longer needed without a scroll area).
5. **Shrink default window size** to 520×680, min 480×500.
6. **Remove vestigial settings widgets** (download_beatmaps_cb, add_to_lazer_cb).
7. **Smoke test on Linux** to confirm rendering. Windows is the user's gate.

The v0.6.1 scaling-fix code (`showEvent` override + `_recompute_scroll_layout`) becomes dead and gets deleted — without the scroll area there's nothing to recompute. The HiDPI rounding policy change from v0.6.1 stays — it's a general-purpose Qt setting unrelated to the scroll fix.

## Error handling

- **Invalid collection IDs** — surfaced via existing `_parse_ids` flow; if all entered IDs are unparseable, show a brief inline error in `status_label` (color: red) instead of `QMessageBox`.
- **Missing CM CLI when "Add to" picker is non-default** — `_on_start` checks and refuses with a status-line message pointing at the Advanced > Paths section.
- **Settings file unparseable** — fall back to defaults, log a one-line warning in the idle log (`[settings] using defaults — settings.json was invalid`).
- **Cancel during merge** — existing behavior (already fail-closed via the merge step's safety checks).

The redesign **does not change any error handling logic**, only where errors are displayed (status line + log box, no more modal popups for inline issues).

## Testing

This is a visual + structural change. Pure unit tests can't cover layout or theme.

**Unit-testable in isolation:**

- `_update_start_enabled` toggles `start_btn.isEnabled()` based on `collection_ids_edit.text()`. One test for empty → False, one for non-empty → True, one for whitespace-only → False.
- `_build_target_combo` populates the picker with the sentinel `"(one collection per osu!collector collection)"` as the first item and `"Don't merge"` as the second.
- `advanced_expanded` is persisted and restored across `_save_settings` / `_load_settings`.

**Manual verification path (Linux):**

1. Launch the app — confirm 520×680 window, all sections visible without scrolling, Advanced collapsed.
2. Paste a collection ID — confirm Start button enables.
3. Click Start — confirm button swaps to Cancel, log strip starts showing per-set lines, progress bar appears and fills cherry-orange.
4. Click Cancel mid-run — confirm worker stops, status line shows "Cancelled", Start re-enables.
5. Click "Advanced" — confirm expander toggles open/closed; all v0.6.x options accessible inside; state persists across relaunches.
6. Resize the window down to 480×500 — confirm no widget gets cut off; below 480×500 the OS clamps to the minimum.

**Manual verification path (Windows — user-driven):**

1. Verify the dark theme renders correctly at 125% / 150% Windows DPI scaling. The HiDPI rounding policy from v0.6.1 should already handle this; the QSS dimensions are in logical pixels so they scale.
2. Confirm the title bar uses native Windows chrome (we don't custom-draw it) and looks reasonable.

## Out of scope

- **Light mode / theme toggle** — single dark theme for v0.7.0; revisit in v0.8.0 if requested.
- **Custom title bar / frameless window** — keep native OS chrome.
- **Icon / logo treatment** — beyond the "by Red" text, no custom logomark.
- **Keyboard shortcuts beyond defaults** — Tab cycles fields, Enter triggers default button, the standard Qt behavior.
- **Drag-and-drop reordering of the Advanced subgroups** — fixed order.
- **Per-user themes / palette customization** — would dilute the "Red's tool" branding.
- **Animation / motion** — fade-in on the Advanced expander would be nice but adds Qt complexity not worth it for v0.7.0.
- **Internationalization** — English-only, hardcoded strings, same as today.

## Open questions resolved during brainstorming

| Question | Resolution |
|---|---|
| Structural approach: tabs / progressive / sidebar? | Single-page progressive (option B) |
| Visual style: lazer pink / Notion / Linear / red? | Red themed (R3D), specifically Cherry (#e3344f) |
| Which red shade? | Cherry (#e3344f → #ffa15f), warm and slightly pink-leaning |
| Include parallelism knobs on main view or in Advanced? | Main view — frequently tweaked per-run |
| Log strip on main view or hidden until run? | Main view, always visible, with idle placeholder |
| Default picker selection? | "(one collection per osu!collector collection)" — preserves v0.6.0 behavior |
| Default window size? | 520×680 (down from today's 900×950) |
| Start button disabled until input? | Yes |
| Light mode toggle? | No — dark-only for v0.7.0 |
| Custom title bar? | No — native OS chrome |
| Drop the QScrollArea? | Yes — the simpler layout fits without scrolling |
