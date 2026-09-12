"""GET /api/system/public-stats — anonymous, aggregates only.

The leak assertion is the load-bearing one. An anonymous endpoint on a
multi-tenant system is exactly where a field gets added later without anyone
re-asking whether it should be public, so the test asserts the SHAPE is closed
rather than only that today's fields are fine.
"""
import pytest
from django.core.cache import cache

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


def test_leaks_no_names_slugs_or_ids(client, db):
    # A count cannot leak what it is a count of. Anything that is not an int
    # could — so the guard is on the type, not on a denylist of field names.
    body = client.get("/api/system/public-stats").json()
    assert not any(isinstance(v, (str, list, dict)) for v in body.values())


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
