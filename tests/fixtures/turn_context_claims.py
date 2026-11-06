"""A seeded world for claiming turns under the Turn Context: a box that confines, a vault value, a person fact and a
prompt that must never reach a record, and the helpers that enqueue, claim and read back. Shared by
`test_turn_context_snapshot` (Stage 2) and `test_turn_context_at_claim` (a queued turn resolved again at its claim);
`claim_world` is registered in the root conftest, and each suite names it `world`."""
from __future__ import annotations

import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.agents.models import AgentCredential
from apps.contacts import people
from apps.contacts import services as contacts_mod
from apps.harness import claim, services
from apps.harness import initiator as who
from apps.harness.models import Runner, RunnerFlag, Turn
from apps.harness.testing import enrolled
from apps.workspaces.models import WorkspaceMembership

from .turn_context import make_world

CONFINING = {"signed_in": True, "profiles": 3, "envelope": 2}
PROMPT = "SEEDEDPROMPTMARKER what are the KC metrics"
FACT = "SEEDEDPERSONFACT leads the KC metrics"
VAULT_VALUE = "zk1:SEEDEDVAULTVALUE"
NOTE = "SEEDEDCONTACTNOTE"
SEEDS = ("SEEDEDPROMPTMARKER", "SEEDEDPERSONFACT", "SEEDEDVAULTVALUE", "SEEDEDCONTACTNOTE", "SEEDEDBLOCKREASON")
_keys = iter(range(10**6))


@pytest.fixture()
def claim_world():
    w = make_world()
    # The owner runs the box: a viewer's box serves no workspace.
    WorkspaceMembership.objects.filter(user=w.owner, workspace=w.ws).update(role=WorkspaceMembership.OWNER)
    AgentCredential.objects.create(agent=w.agent, name="SEEDED_NAME", value_zk=VAULT_VALUE)
    w.box = _box(w, "box-one")
    return w


def _box(w, name, **caps):
    return enrolled(Runner.objects.create(
        name=name, kind=Runner.CLOUD, host=name, owner=w.owner, status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        ready=True, capabilities={"sessions": True, "midturn": 1, "clis": {"claude-code": CONFINING}, **caps}))


def _sensitive_box(w):
    box = _box(w, "sensitive-box", data_mode="sensitive_data")
    RunnerFlag.objects.create(runner=box, flag="sensitive_data", declared_by=w.owner)
    return box


def _seed_fact(w, user):
    """A fact the person has made available, so the envelope's `person` block really carries it."""
    response = _browser(user).put("/api/people/me/agent-memory/", {"use": {"available": True, "default": True}},
                                  content_type="application/json")
    assert response.status_code == 200, response.content
    return people.record_fact(person=contacts_mod.person_for(user=user), workspace=w.ws, kind="role", statement=FACT, basis="declared")


def _enqueue(user, session=None, *, agent=None, box=None, capability=None, prompt=PROMPT, origin=Turn.ORIGIN_CANOPY_WEB_CHAT):
    turn, _ = services.enqueue_turn(
        **({"session": session} if session is not None else {"agent": agent}), origin=origin, prompt=prompt,
        idempotency_key=f"s-{next(_keys)}", capability=capability, pinned_runner=box,
        initiator=who.for_user(user, via=origin, assurance=who.SESSION))
    return turn


def _claim(turn, box):
    got = claim.claim_next_turn(box)
    assert got is not None and got.pk == turn.pk, f"{turn.pk} was not claimed: {turn.status} {turn.result_note}"
    return got


def _browser(user):
    c = Client()
    c.force_login(user)
    return c


def _context(user, session, c=None):
    return (c or _browser(user)).get(f"/api/canopy-sessions/{session.pk}/context")


def _ran_as(user, session, turn, c=None):
    return (c or _browser(user)).get(f"/api/canopy-sessions/{session.pk}/turns/{turn.pk}/context")


def _keys_in(doc):
    if isinstance(doc, dict):
        for key, value in doc.items():
            yield key
            yield from _keys_in(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from _keys_in(value)


def _assert_clean(doc, *values):
    text = json.dumps(doc, default=str)
    for seed in (*SEEDS, *values):
        assert seed not in text, seed
    assert "person" not in set(_keys_in(doc))
