"""GET /api/system/public-stats — anonymous, aggregates only.

The leak assertion is the load-bearing one. An anonymous endpoint on a
multi-tenant system is exactly where a field gets added later without anyone
re-asking whether it should be public, so the test asserts the SHAPE is closed
rather than only that today's fields are fine.
"""
import pytest
from django.core.cache import cache
from django.test import Client, override_settings

ALLOWED_KEYS = {
    "agents",
    "skills",
    "runners_online",
    "turns_executed",
    "demos_published",
}


@pytest.fixture(autouse=True)
def _clear_public_stats_cache():
    # public_stats() caches for 60s (apps/system/stats.py) so an anonymous
    # caller can't turn this into a load generator — but that means a value
    # computed by an EARLIER test would otherwise leak into a later one within
    # the same process. Same pattern as test_attach_registry.py /
    # test_token_exchange.py.
    cache.clear()
    yield
    cache.clear()


def test_reachable_without_authentication(client, db):
    resp = client.get("/api/system/public-stats")
    assert resp.status_code == 200, resp.content


def test_returns_only_integer_aggregates(client, db):
    body = client.get("/api/system/public-stats").json()
    assert set(body) == ALLOWED_KEYS
    for key, value in body.items():
        assert isinstance(value, int), f"{key} is {type(value)}, not an int"


@override_settings(REQUIRE_AUTH=True)
def test_public_stats_is_public_through_the_login_middleware(db):
    # The allowlist half of the gate. REQUIRE_AUTH is False suite-wide
    # (config/settings/test.py:24), so without this override
    # LoginRequiredMiddleware is inert and the whole suite proves only that
    # auth=None took on the route — apps/common/middleware.py's
    # PUBLIC_PATH_PREFIXES entry could be deleted and every other test here
    # would stay green while production 302s every anonymous visitor to
    # Google. This is the test that actually exercises that allowlist.
    assert Client().get("/api/system/public-stats").status_code == 200


@override_settings(REQUIRE_AUTH=True)
def test_sibling_system_routes_are_not_public(db):
    # PUBLIC_PATH_PREFIXES is a PREFIX match (middleware._is_public), so it
    # also admits /api/system/public-stats/foo — which Ninja routes to
    # detail(kind="public-stats", name="foo"), not public_stats(). Pin that
    # Ninja's own session_auth still gates that path, since the middleware
    # allowlist does not distinguish it from the real public route.
    assert Client().get("/api/system/public-stats/foo").status_code == 401


def test_leaks_no_names_slugs_or_ids(client, db):
    # A count cannot leak what it is a count of — but that claim is only
    # tested if something with a name/slug/id actually exists in the DB and
    # is confirmed absent from the response, and if the response can't
    # authenticate an anonymous caller into a session either. Asserting only
    # "every value is an int" (test_returns_only_integer_aggregates) already
    # implies this — and isinstance(True, int) is True, so that assertion is
    # even weaker than it reads — so this test earns its name by checking the
    # actual leak claim instead of restating the type check.
    from apps.agents.models import Agent
    from apps.workspaces.testing import a_workspace

    ws = a_workspace(slug="leak-canary-workspace")
    Agent.objects.create(slug="leak-canary-agent", name="Leak Canary", workspace=ws)

    # A bare integer pk is deliberately NOT checked here: with a response this
    # small (five single-digit counts), a small autoincrement id is likely to
    # coincidentally match one of them, which would make this assertion flaky
    # rather than meaningful. The distinctive slugs/name are the real signal.
    resp = client.get("/api/system/public-stats")
    raw = resp.content.decode()
    assert "leak-canary-workspace" not in raw  # Workspace.slug (its pk)
    assert "leak-canary-agent" not in raw
    assert "Leak Canary" not in raw
    # An anonymous read must not mint or extend a session either.
    assert "Set-Cookie" not in resp.headers


def test_counts_reflect_reality(client, db):
    from apps.agents.models import Agent
    from apps.workspaces.testing import a_workspace

    # Workspace.created_by is NOT NULL, so a bare Workspace.objects.create(...)
    # (no created_by) raises IntegrityError on an in-memory test DB — use the
    # shared tenancy fixture helper (apps/workspaces/testing.py) instead.
    ws = a_workspace(slug="acme")
    Agent.objects.create(slug="a1", name="A1", workspace=ws)
    Agent.objects.create(slug="a2", name="A2", workspace=ws)

    body = client.get("/api/system/public-stats").json()
    assert body["agents"] == 2


def test_demos_published_comes_from_the_registry(client, db):
    from django.contrib.auth import get_user_model

    from apps.walkthroughs.models import Walkthrough

    owner = get_user_model().objects.create_user(username="o", email="o@dimagi.com")

    def artifact(kind: str) -> None:
        # Walkthrough has SEVEN fields with neither null/blank nor a default
        # (verified against apps/walkthroughs/models.py): title, kind, owner,
        # drive_file_id, drive_folder_id, content_type, size_bytes. Omitting any
        # of them raises IntegrityError, not a validation error.
        Walkthrough.objects.create(
            title=f"artifact-{kind}",
            kind=kind,
            owner=owner,
            drive_file_id=f"file-{kind}",
            drive_folder_id="folder-1",
            content_type="video/mp4" if kind == "video" else "text/html",
            size_bytes=1,
            run_id="demo-2026-09-12-001",
        )

    # Two artifacts, ONE run package — the count must be 1, not 2. This is the
    # assertion that catches counting rows instead of distinct run_ids.
    artifact("video")
    artifact("html")

    body = client.get("/api/system/public-stats").json()
    assert body["demos_published"] == 1


def test_demos_published_ignores_artifacts_with_no_run(client, db):
    from django.contrib.auth import get_user_model

    from apps.walkthroughs.models import Walkthrough

    owner = get_user_model().objects.create_user(username="o2", email="o2@dimagi.com")
    # A one-off upload carries no run_id — it is not a published package.
    Walkthrough.objects.create(
        title="loose", kind="html", owner=owner, drive_file_id="f2",
        drive_folder_id="folder-1", content_type="text/html", size_bytes=1,
    )

    body = client.get("/api/system/public-stats").json()
    assert body["demos_published"] == 0
