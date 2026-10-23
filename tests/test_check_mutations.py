"""The mutation checker must fail a mutation whose anchor left the code."""
from __future__ import annotations

from scripts.check_mutations import RUNTIME, Mutation, check


def test_missing_anchor_fails_without_touching_the_file():
    stale = Mutation("stale", "fast", RUNTIME, "not in the source\n", "", "tests", "x")

    assert check(stale) == "anchor not found"
