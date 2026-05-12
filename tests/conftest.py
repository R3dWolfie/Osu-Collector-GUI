"""Pytest config: make the repo root importable so tests can `import osu_collector_gui`."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_beatmap_mirror_state():
    """Clear BeatmapMirror's process-wide dead-cache and active-count
    state between tests so class-level state doesn't bleed."""
    from osu_collector_gui import BeatmapMirror
    BeatmapMirror.reset_state()
    yield
    BeatmapMirror.reset_state()
