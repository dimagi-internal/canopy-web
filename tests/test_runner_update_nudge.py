"""The update nudge: a heartbeat that reports a stale code_sha rings the box's
updater NOW instead of leaving it to the 30-minute timer.

The server is the only party that sees both shas the moment they diverge (a
deploy moves `expected_code_sha`; the very next heartbeat reports the old
installed sha), so it publishes `runner.update_available` down the control
channel the runner already holds. The daemon never installs anything on this
signal — it kickstarts the separate updater job, which independently re-checks
staleness and the in-flight marker. The 30-min timer survives as the rescue
path for a daemon too dead to hear a frame (the same push-is-the-doorbell,
poll-is-the-auditor shape as Gmail inbound).
"""
from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.harness import services
from apps.harness.models import Runner

pytestmark = pytest.mark.django_db

LAPTOP_SHA = "1111111111111111111111111111111111111111"
CLOUD_SHA = "2222222222222222222222222222222222222222"
OLD_SHA = "9999999999999999999999999999999999999999"


@pytest.fixture
def shas(settings):
    settings.RUNNER_CODE_SHA = LAPTOP_SHA
    settings.RUNNER_CLOUD_CODE_SHA = CLOUD_SHA


def _runner(kind: str = Runner.EMDASH) -> Runner:
    user = User.objects.create_user(f"u-{kind}", f"{kind}@dimagi.com", "pw")
    return Runner.objects.create(
        name=f"box-{kind}", kind=kind, paired_by=user,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )


def test_stale_heartbeat_publishes_update_available(shas):
    r = _runner()
    with mock.patch("apps.realtime.groups.publish") as pub:
        services.heartbeat(r, active_turn_ids=[], code_sha=OLD_SHA)
    frames = [c.args[1] for c in pub.call_args_list
              if c.args[1].get("type") == "runner.update_available"]
    assert frames == [{"type": "runner.update_available", "expected_sha": LAPTOP_SHA}]


def test_current_heartbeat_stays_quiet(shas):
    r = _runner()
    with mock.patch("apps.realtime.groups.publish") as pub:
        services.heartbeat(r, active_turn_ids=[], code_sha=LAPTOP_SHA)
    assert not any(c.args[1].get("type") == "runner.update_available"
                   for c in pub.call_args_list)


def test_cloud_runner_is_compared_against_the_cloud_sha(shas):
    # The fleet runs two programs; nudging a cloud box because it doesn't match
    # the LAPTOP's sha would ring it forever.
    r = _runner(Runner.CLOUD)
    with mock.patch("apps.realtime.groups.publish") as pub:
        services.heartbeat(r, active_turn_ids=[], code_sha=CLOUD_SHA)
    assert not any(c.args[1].get("type") == "runner.update_available"
                   for c in pub.call_args_list)


def test_empty_expected_means_unknown_never_stale(settings):
    # A dev server bakes in no expectation. Empty must stay silent — nudging on
    # it would ring every runner on every heartbeat forever.
    settings.RUNNER_CODE_SHA = ""
    r = _runner()
    with mock.patch("apps.realtime.groups.publish") as pub:
        services.heartbeat(r, active_turn_ids=[], code_sha=OLD_SHA)
    assert not any(c.args[1].get("type") == "runner.update_available"
                   for c in pub.call_args_list)


def test_empty_reported_means_unknown_never_stale(shas):
    # An unstamped install reports no sha; that is UNKNOWN, not "different".
    r = _runner()
    with mock.patch("apps.realtime.groups.publish") as pub:
        services.heartbeat(r, active_turn_ids=[], code_sha="")
    assert not any(c.args[1].get("type") == "runner.update_available"
                   for c in pub.call_args_list)
