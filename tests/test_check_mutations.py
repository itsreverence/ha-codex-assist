"""The mutation checker must report each mutation that its test does not catch."""
from __future__ import annotations

from scripts.check_mutations import Mutation, caught, check


def test_passing_test_does_not_catch_even_with_the_expected_text():
    assert not caught(0, "NoRefreshReuse", "NoRefreshReuse")


def test_failure_without_the_expected_text_does_not_catch():
    assert not caught(1, "StoredTokenUsable", "NoRefreshReuse")


def test_failure_with_the_expected_text_catches():
    assert caught(1, "violated NoRefreshReuse", "NoRefreshReuse")


def test_missing_anchor_reports_missing(tmp_path):
    (tmp_path / "source.py").write_text("value = 1\n")
    stale = Mutation("stale", "source.py", "value = 2", "", "tests", "x")

    assert check(stale, tmp_path) == "anchor missing"


def test_repeated_anchor_reports_ambiguous_with_count(tmp_path):
    (tmp_path / "source.py").write_text("value = 1\nvalue = 1\n")
    repeated = Mutation("repeated", "source.py", "value = 1", "", "tests", "x")

    assert check(repeated, tmp_path) == "anchor ambiguous (2 matches)"
