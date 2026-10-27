"""Fixtures for the agent-task suites."""
from __future__ import annotations

import pytest

from .helpers import make_world


@pytest.fixture()
def world(db):
    return make_world()
