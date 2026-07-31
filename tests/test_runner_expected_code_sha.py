"""`expected_code_sha` is per RUNNER KIND, because the fleet runs two programs.

A laptop executes `runner/canopy_runner/canopy_runner`; a cloud box executes
`runner/ec2`. One sha cannot describe both, and serving the laptop's to a cloud
runner would mark every cloud box permanently stale — noise on exactly the rows
the alert was extended to cover.

See docs/superpowers/specs/2026-07-30-cloud-runner-auto-update-design.md.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.harness.models import Runner
from apps.harness.schemas import RunnerOut

pytestmark = pytest.mark.django_db

LAPTOP_SHA = "1111111111111111111111111111111111111111"
CLOUD_SHA = "2222222222222222222222222222222222222222"


@pytest.fixture
def shas(settings):
    settings.RUNNER_CODE_SHA = LAPTOP_SHA
    settings.RUNNER_CODE_COMMITTED_AT = 1753000000
    settings.RUNNER_CLOUD_CODE_SHA = CLOUD_SHA
    settings.RUNNER_CLOUD_CODE_COMMITTED_AT = 1754000000


def _runner(kind: str) -> Runner:
    user = User.objects.create_user(f"u-{kind}", f"{kind}@dimagi.com", "pw")
    return Runner.objects.create(
        name=f"box-{kind}", kind=kind, paired_by=user,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )


def test_cloud_runner_is_told_the_cloud_sha(shas):
    out = RunnerOut.from_orm(_runner(Runner.CLOUD))
    assert out.expected_code_sha == CLOUD_SHA
    assert out.expected_code_committed_at == 1754000000


def test_laptop_runner_is_told_the_laptop_sha(shas):
    out = RunnerOut.from_orm(_runner(Runner.EMDASH))
    assert out.expected_code_sha == LAPTOP_SHA
    assert out.expected_code_committed_at == 1753000000


def test_a_remote_runner_keeps_the_laptop_expectation(shas):
    # `remote` runs the same package as `emdash` (services.session_capable groups
    # them). Only `cloud` is the other program.
    out = RunnerOut.from_orm(_runner(Runner.REMOTE))
    assert out.expected_code_sha == LAPTOP_SHA


def test_an_unset_cloud_sha_is_unknown_not_the_laptops(settings):
    # A build without the new arg (a dev image, a local build) must degrade to
    # UNKNOWN — which is silent — never borrow the other runner's sha, which would
    # differ from what every cloud box reports and alert on all of them forever.
    settings.RUNNER_CODE_SHA = LAPTOP_SHA
    settings.RUNNER_CLOUD_CODE_SHA = ""
    assert RunnerOut.from_orm(_runner(Runner.CLOUD)).expected_code_sha == ""


def test_a_malformed_committed_at_does_not_500_the_list(settings):
    settings.RUNNER_CLOUD_CODE_COMMITTED_AT = "not-an-int"
    assert RunnerOut.from_orm(_runner(Runner.CLOUD)).expected_code_committed_at == 0
