"""Shared test fixtures — currently just the tenancy baseline.

`Agent.workspace` is NOT NULL as of `agents/0013`, so creating an Agent now
requires a Workspace to home it in, and a Workspace requires a `created_by`
User. Every real deployment satisfies both by construction (you cannot reach
`POST /api/agents/` without being logged in, and the login is what makes the
default workspace exist), but an in-memory test DB starts with neither — which
is why `wsvc.ensure_default_workspace()` returns `None` in a bare test and why
a fixture that passed its result straight to `Agent.objects.create` used to
quietly produce the very unhomed agent the constraint exists to forbid.

Depend on `default_workspace` from any fixture that needs a tenant. Fixtures
that already call `wsvc.ensure_default_workspace()` only need to take it as a
parameter — with a user present, that call starts returning a real workspace.
Plain (non-fixture) helper functions call `apps.workspaces.testing.a_workspace()`
directly instead; both go through the same code.

Deliberately NOT autouse: tests that assert on the empty-database case (the
workspace-backfill migration tests, `ensure_default_workspace()` returning
None) must keep seeing an empty database.
"""
from __future__ import annotations

import pytest

# Fixtures more than one suite shares live in tests/fixtures/<area>.py and are
# registered here, so no test module imports another one (a guard enforces it:
# tests/test_no_test_module_imports.py). Plain helpers and constants there are
# imported by name; fixtures resolve by name with no import. An autouse fixture
# never moves here — a suite that wants one asks for it by name
# (`pytest.mark.usefixtures`), so it cannot quietly apply to every test.
pytest_plugins = [
    "tests.fixtures.aws_proof",
    "tests.fixtures.people_brain",
    "tests.fixtures.self_host",
    "tests.fixtures.slack",
    "tests.fixtures.slack_channel_ingest",
]


@pytest.fixture
def default_workspace(db):
    """The default (`dimagi`) workspace, created the way the app creates it.

    Returned as the Workspace instance; `wsvc.ensure_default_workspace()` finds
    the same row, so a fixture can keep calling that and simply depend on this.
    """
    from apps.workspaces.testing import a_workspace

    return a_workspace()


@pytest.fixture(autouse=True)
def _settings_are_restored():
    """Fail the test that LEAKS a settings change, not the unlucky one after it.

    `@override_settings` on a test that also writes through pytest-django's
    `settings` fixture unwinds out of order: the decorator restores first, then
    the fixture restores the snapshot it took INSIDE the override — so the
    override outlives the test. That left REQUIRE_AUTH on for the rest of an
    xdist worker, and whichever anonymous API test ran next there failed with
    the login middleware's 401 (tests/test_public_site.py, 2026-10-17). Use one
    mechanism per test: the `settings` fixture.
    """
    from django.conf import settings

    before = settings.REQUIRE_AUTH
    yield
    assert settings.REQUIRE_AUTH == before, (
        f"this test left settings.REQUIRE_AUTH={settings.REQUIRE_AUTH!r} (was {before!r}) — "
        "don't mix @override_settings with writes through the `settings` fixture")


@pytest.fixture(autouse=True)
def _no_live_dns(monkeypatch):
    """No test reaches a real DNS resolver. The interface's mail-domain warning
    (apps/contacts/domain_proof.py) asks `_dmarc.<domain>` on every interface
    read; here every lookup answers "unknown", which raises no warning. Tests of
    that warning patch `_lookup_dmarc` themselves."""
    try:
        from apps.contacts import domain_proof
    except Exception:  # a suite with no Django (runner/*) has nothing to patch
        return
    monkeypatch.setattr(domain_proof, "_lookup_dmarc", lambda domain: "unknown")
    try:
        from apps.mail import dns as mail_dns
    except Exception:
        return
    monkeypatch.setattr(mail_dns, "_resolve", lambda name, rtype: [])


@pytest.fixture(autouse=True)
def _no_live_plugin_json(monkeypatch):
    """No test reaches GitHub for a plugin.json name (apps/plugins/services.py):
    the agents API names each agent's plugin, so every agent list would. Here
    every repo reads as private (404 -> the repo name). The catalog tests patch
    `_raw_get` themselves."""
    try:
        from apps.plugins import services as plugin_services
    except Exception:
        return

    class _NotFound:
        status_code = 404

    monkeypatch.setattr(plugin_services, "_raw_get", lambda url: _NotFound())


@pytest.fixture()
def agent_memory_on():
    """Every Person created while this is active starts with BOTH agent-memory
    features available and ON by default (`Person.hcp_*_available` / `_default`;
    the real default is off).
    For suites that pin what the people brain / HCP do for a person who has
    turned them on."""
    from django.db.models.signals import pre_save

    from apps.contacts.models import Person

    def _on(sender, instance, **kwargs):
        if instance._state.adding:
            instance.hcp_record_available = instance.hcp_record_default = True
            instance.hcp_use_available = instance.hcp_use_default = True

    pre_save.connect(_on, sender=Person, dispatch_uid="test-agent-memory-on")
    yield
    pre_save.disconnect(sender=Person, dispatch_uid="test-agent-memory-on")


@pytest.fixture()
def grants_on_turn():
    """As if every person had already granted ("always") each agent they start a
    turn with, for whatever they have made available at that moment — through the
    real act (`hcp.issue_agent_grant`), so the grant rows and `grant.issued` events
    are the real ones. For suites that pin what a GRANTED agent does; how a grant
    comes to exist is pinned in tests/test_hcp_agent_grants.py."""
    from django.db.models.signals import post_save

    from apps.contacts import hcp, people
    from apps.harness.models import Turn

    def _grant(sender, instance, created, **kwargs):
        if not created:
            return
        agent = (instance.agent if instance.agent_id else
                 instance.chat_session.agent if instance.chat_session_id else None)
        person = people.initiator_person(instance) if agent is not None else None
        if person is None:
            return
        features = [f for f in hcp.FEATURES if getattr(person, f"hcp_{f}_available")]
        have = hcp.granted(person, agent)
        if any(not have[f] for f in features):
            actor = (hcp.user_actor(person.user) if person.user_id
                     else hcp.Actor(f"person:{person.pk}", "user"))
            hcp._record_grant(person, agent=agent, features=features,
                              sources=hcp.agent_sources(agent),
                              gtype="persistent", actor=actor, surface="chat")

    post_save.connect(_grant, sender=Turn, dispatch_uid="test-grants-on-turn")
    yield
    post_save.disconnect(sender=Turn, dispatch_uid="test-grants-on-turn")


@pytest.fixture()
def agents_granted(agent_memory_on, grants_on_turn):
    """Agent memory on for every person, and every agent they talk to granted."""
    yield


def _shard_of(files: dict[str, int], shards: int) -> dict[str, int]:
    """Each test file's shard, balanced by how many tests it holds: largest file
    first onto the lightest shard. Deterministic from the collection alone, so
    every shard (and every xdist worker inside one) computes the same split, and
    their union is the whole suite by construction — no file can fall between
    shards the way a hand-kept list can."""
    load = [0] * shards
    out: dict[str, int] = {}
    for path, n in sorted(files.items(), key=lambda kv: (-kv[1], kv[0])):
        i = load.index(min(load))
        out[path] = i
        load[i] += n
    return out


def pytest_collection_modifyitems(config, items):
    """`PYTEST_SHARD=i/N` (CI's `Backend pytest` matrix) keeps only shard i of N.

    The suite is ~5 minutes on one runner even with `-n auto`; split across N
    runners it is ~5/N, which is most of the merge queue's wait. Unset (local
    runs), nothing changes."""
    import os

    spec = os.environ.get("PYTEST_SHARD")
    if not spec:
        return
    index, total = (int(x) for x in spec.split("/"))
    files: dict[str, int] = {}
    for item in items:
        files[item.nodeid.split("::")[0]] = files.get(item.nodeid.split("::")[0], 0) + 1
    shard = _shard_of(files, total)
    keep = [it for it in items if shard[it.nodeid.split("::")[0]] == index - 1]
    drop = [it for it in items if shard[it.nodeid.split("::")[0]] != index - 1]
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
