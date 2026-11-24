"""A token's last use is stamped at most once a minute (`apps/tokens/models.stamp_last_used`),
not on every request a runner or agent makes."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.tokens import principal as P
from apps.tokens.models import LAST_USED_RESOLUTION_S, PersonalToken, note_used, stamp_last_used

pytestmark = pytest.mark.django_db


@pytest.fixture()
def pat():
    user = User.objects.create_user("u", "u@dimagi.com", "pw")
    return PersonalToken.create_for_user(user=user, label="cli")


def _updates(ctx):
    return [q for q in ctx.captured_queries if q["sql"].lstrip().upper().startswith("UPDATE")]


def test_a_second_use_within_the_minute_writes_nothing(pat):
    raw, _ = pat
    P.resolve_bearer(raw, client="canopy-cli")
    with CaptureQueriesContext(connection) as ctx:
        assert P.resolve_bearer(raw, client="canopy-cli") is not None
    assert _updates(ctx) == []


def test_a_use_after_the_minute_is_stamped(pat):
    raw, token = pat
    old = timezone.now() - timedelta(seconds=LAST_USED_RESOLUTION_S + 5)
    PersonalToken.objects.filter(pk=token.pk).update(last_used_at=old, last_used_client="canopy-cli")
    P.resolve_bearer(raw, client="canopy-cli")
    token.refresh_from_db()
    assert token.last_used_at > old


def test_a_new_client_is_recorded_at_once(pat):
    raw, token = pat
    P.resolve_bearer(raw, client="canopy-cli")
    P.resolve_bearer(raw, client="canopy-runner")
    token.refresh_from_db()
    assert token.last_used_client == "canopy-runner"


def test_the_update_is_conditional_so_a_stale_in_memory_copy_does_not_rewrite(pat):
    """Another task stamped it a moment ago; this process's copy still says never."""
    _, token = pat
    recent = timezone.now() - timedelta(seconds=5)
    PersonalToken.objects.filter(pk=token.pk).update(last_used_at=recent)
    token.last_used_at = None
    stamp_last_used(PersonalToken, token)
    assert PersonalToken.objects.get(pk=token.pk).last_used_at == recent


def test_note_used_first_use_stamps(pat):
    _, token = pat
    note_used(token, "x")
    token.refresh_from_db()
    assert token.last_used_at is not None and token.last_used_client == "x"


def test_runner_tokens_go_through_the_throttle():
    from tests._source import ROOT

    src = (ROOT / "apps/tokens/middleware.py").read_text()
    assert "stamp_last_used(RunnerAccessToken, rtok)" in src
    assert "update(last_used_at=" not in src
