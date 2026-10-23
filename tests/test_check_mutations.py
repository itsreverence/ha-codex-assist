"""The mutation checker must report each mutation that its test does not catch."""
from __future__ import annotations

import pytest

from scripts.check_mutations import Mutation, check

SOURCE = "def value():\n    # the contract\n    return 1\n"


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["."]\n')
    (tmp_path / "mod.py").write_text(SOURCE)
    (tmp_path / "test_mod.py").write_text(
        "from mod import value\n\n\ndef test_value():\n"
        "    assert value() == 1, 'value contract broken'\n"
    )
    return tmp_path


def _mutation(old, new, expect="value contract broken"):
    return Mutation(name="m", path="mod.py", old=old, new=new, test="test_mod.py", expect=expect)


def test_mutation_caught_by_its_expected_failure_restores_the_file(tree):
    assert check(_mutation("return 1", "return 2"), tree) == ("caught", "")
    assert (tree / "mod.py").read_text() == SOURCE


def test_mutation_the_test_passes_is_reported_with_its_output(tree):
    status, output = check(_mutation("# the contract", "# changed"), tree)

    assert status == "SURVIVED (test passed)"
    assert "1 passed" in output


def test_failure_without_the_expected_text_is_a_wrong_failure(tree):
    status, output = check(_mutation("return 1", "return 2", expect="other contract"), tree)

    assert status == "WRONG FAILURE (expected text not found)"
    assert "value contract broken" in output
    assert (tree / "mod.py").read_text() == SOURCE


def test_missing_anchor_reports_missing(tree):
    assert check(_mutation("return 3", ""), tree) == ("anchor missing", "")


def test_repeated_anchor_reports_ambiguous_with_count(tree):
    (tree / "mod.py").write_text(SOURCE + SOURCE)

    assert check(_mutation("return 1", ""), tree) == ("anchor ambiguous (2 matches)", "")
