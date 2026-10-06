"""Who still calls the old address is recorded, so retiring it is a decision
made from evidence (apps/common/legacy_traffic.py)."""
import pytest
from django.core.cache import cache

from apps.common import legacy_traffic
from apps.events.models import Event
from apps.tokens.models import PersonalToken

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(default_workspace):
    from django.contrib.auth.models import User

    from apps.workspaces import services as wsvc
    from apps.workspaces.models import WorkspaceMembership

    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    wsvc.ensure_member(default_workspace, u, WorkspaceMembership.EDITOR)
    return u


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def test_ids_are_taken_out_of_the_surface():
    assert legacy_traffic.surface(
        "/api/harness/runners/c6332c59-54b2-4943-a65a-538c47bf6329/heartbeat") == \
        "/api/harness/runners/:id"


def test_a_call_is_recorded_once_a_minute_with_who_and_where(default_workspace, user):
    for _ in range(3):
        legacy_traffic.note(path="/api/harness/runners/abc12345-0000/heartbeat", user=user,
                            credential={"label": "jj-laptop"}, client="canopy-runner")
    rows = list(Event.objects.filter(source=legacy_traffic.SOURCE))
    assert len(rows) == 1
    assert rows[0].payload["who"] == "jj-laptop"
    assert rows[0].payload["client"] == "canopy-runner"


def test_the_load_balancers_health_check_is_not_a_caller(default_workspace):
    legacy_traffic.note(path="/health/", user=None)
    assert not Event.objects.filter(source=legacy_traffic.SOURCE).exists()


def test_a_request_on_the_old_prefix_is_recorded_end_to_end(client, default_workspace, user, settings):
    """Through the real middleware: the scope marker the ASGI prefix-stripper
    sets is what identifies the old address."""
    from config.asgi_prefix import SCOPE_KEY

    raw, _ = PersonalToken.create_for_user(user=user, label="old-runner")
    resp = client.get("/api/harness/runners/", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 200
    assert not Event.objects.filter(source=legacy_traffic.SOURCE).exists()   # the new address: not recorded

    from django.test import RequestFactory

    from apps.common.legacy_prefix import LegacyPrefixMiddleware

    request = RequestFactory().get("/api/harness/runners/", HTTP_X_CANOPY_CLIENT="canopy-runner")
    request.scope = {SCOPE_KEY: "/canopy"}
    request.user = user
    request.auth_credential = {"label": "old-runner"}
    from django.urls import set_script_prefix

    try:
        LegacyPrefixMiddleware(lambda r: type("R", (), {"status_code": 200})())(request)
    finally:
        # The middleware sets the thread's script prefix (its job for a real
        # old-address request); left set, it leaks `/canopy` into later tests.
        set_script_prefix("/")
    row = Event.objects.get(source=legacy_traffic.SOURCE)
    assert row.payload["who"] == "old-runner"
