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
