"""A runner's heartbeat keeps Runner.platform true (it was set only at pairing)."""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.harness import runner_protocol
from apps.harness.models import Runner
from apps.harness.schemas import HeartbeatIn

pytestmark = pytest.mark.django_db


def _runner(**kw):
    return Runner.objects.create(name="r", kind=Runner.EMDASH, status=Runner.ONLINE,
                                 last_heartbeat_at=timezone.now(), **kw)


def test_a_beat_records_the_platform_the_runner_reports():
    runner = _runner(platform="")
    runner_protocol.heartbeat(runner, HeartbeatIn(platform="windows"))
    runner.refresh_from_db()
    assert runner.platform == "windows"


@pytest.mark.parametrize("sent", ["", "beos"])
def test_an_absent_or_unknown_platform_leaves_it_alone(sent):
    runner = _runner(platform="macos")
    runner_protocol.heartbeat(runner, HeartbeatIn(platform=sent))
    runner.refresh_from_db()
    assert runner.platform == "macos"
