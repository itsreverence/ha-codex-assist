"""Fixtures for smoke tests that run against a real Home Assistant instance.

This suite is separate from ``tests/`` (which uses lightweight fakes) and
requires ``requirements_test_ha.txt`` to be installed. See docs/TESTING.md.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow Home Assistant to load custom_components/ from this repo."""
    yield
