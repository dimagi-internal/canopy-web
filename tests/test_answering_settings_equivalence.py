"""The real code answers exactly as `tests/legacy_interface.py` does, for every agent shape.

`docs/superpowers/specs/2026-11-04-agent-settings-replace-the-interface-design.md` § 6. Both read
`Agent.interface`, and the plain settings keep it as the storage, so this holds in every phase: it is
what checks the new forms' writer, and the eventual deletion of `pages`, against today.

A failure here means an agent would answer someone differently. The fix is in the code that changed,
not in the frozen copy; `FROZEN_ANSWERS` catches the two being edited together.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import re
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from apps.agents import answering_snapshot, grants, interface, roles, sensitive_data, session_context, turn_context
from apps.agents.answering_snapshot import probe_turn
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.canopy_sessions.models import Session
from apps.contacts.models import Contact
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner, RunnerAssignment, RunnerFlag, Turn
from apps.harness.testing import enrolled
from apps.host_grants import execution, site_agents
from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership

from . import legacy_decide, legacy_interface

pytestmark = pytest.mark.django_db

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "answering_interfaces.json"
BASE_URL = "https://canopy.test"
RETIRED = "retired_probe"

#: The hash of every answer the frozen copy gives over the fixture. It moves only if the frozen
#: copy or the fixture moves, and neither should: see the module docstring.
FROZEN_ANSWERS = "1877f8032a283484c7a5ec18e667fe5cba2eb51e24006dd9fd68b1cc7acc657f"

def _as_decision(ctx):
    """A Turn Context in the frozen copy's shape, so the two compare field by field."""
    answer = answering_snapshot.answer_of(ctx)
    del answer["stamp"]
    return legacy_decide.Decision(**{**answer, "permissions": frozenset(answer["permissions"])})


def _real_decide(agent, who, *, verified, capability, turn):
    return _as_decision(turn_context.for_asker(agent, who, verified=verified, requested=capability, turn=turn))


def _envelope_profile(agent, stamp, turn):
    """What the envelope hands the runner for a stamp: the Turn Context's profile."""
    if agent is None or not stamp:
        return None
    facts = session_context.build(None, turn, agent=agent)
    return turn_context.stamped_profile(agent, stamp, facts).as_envelope()


REAL = SimpleNamespace(
    profile=interface.profile, envelope_profile=_envelope_profile, reachable=interface.reachable,
    page_sites=interface.page_sites, site_ceiling=interface.site_ceiling, own_login_of=interface.own_login_of,
    page_turn=interface.page_turn, page_site_of=interface.page_site_of,
    handles_sensitive=sensitive_data.handles_sensitive, as_claim=sensitive_data.as_claim,
    sensitive_capabilities=sensitive_data.sensitive_profiles, facts_for=execution.facts_for,
    site_capabilities=site_agents.site_profiles, agent_entry=site_agents.agent_entry, decide=_real_decide,
)


def _keys(agent) -> list[str]:
    return list((agent.interface or {}).get("capabilities") or {})


def _sites(agent) -> list[str]:
    return sorted(interface.page_sites(agent))


@pytest.fixture()
def world():
    data = json.loads(FIXTURE.read_text())
    user_model = get_user_model()
    make = lambda name: user_model.objects.create_user(name, f"{name}@dimagi-ai.com", "pw", first_name=name.title())  # noqa: E731
    owner, admin, editor, reader, member, publisher = map(make, ("owner", "admin", "editor", "reader", "member", "ada"))
    agents, workspaces = [], {}
    for row in [*data["agents"], *data["corner_cases"]]:
        ws = workspaces.get(row["workspace"])
        if ws is None:
            ws = workspaces[row["workspace"]] = Workspace.objects.create(
                slug=row["workspace"], display_name=row["workspace"], created_by=owner)
            WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.ADMIN)
            for person in (admin, editor, reader, member):
                WorkspaceMembership.objects.create(user=person, workspace=ws, role=WorkspaceMembership.VIEWER)
        agent = Agent.objects.create(
            slug=row["slug"], name=row["slug"], workspace=ws, owner=owner, email=row.get("email", ""),
            interface=row["interface"], interface_source="",
            interface_published_by=publisher if row.get("published_by") else None,
            interface_published_at=dt.datetime.fromisoformat(row["published_at"]) if row.get("published_at") else None)
        AgentAdmin.objects.create(agent=agent, user=admin)
        AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=editor, role=roles.EDITOR)
        AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=reader, role=roles.READ_ONLY)
        for key in _keys(agent):
            # One domain per name, so each contact below holds that one `use:<name>` and nothing else.
            AgentGrant.objects.create(agent=agent, kind=AgentGrant.CONTACT, capability=key,
                                      domain=f"{key}.contacts.example", verified=False)
        agents.append((agent, row))
    apps = {}
    for agent, _row in agents:
        for site in _sites(agent):
            key = (agent.workspace_id, site)
            if key not in apps:
                apps[key] = AppCredential.create_credential(name=site, created_by=owner, workspace=agent.workspace)
    # One ordinary box and one sensitive box, so an entry's runner half is not all "none".
    ordinary = _box(owner, "ordinary")
    sensitive = _box(owner, "sensitive", sensitive=True)
    for agent, _row in agents:
        box = sensitive if sensitive_data.sensitive_profiles(agent.interface) else ordinary
        RunnerAssignment.objects.create(agent=agent, runner=box, rank=1, enabled=True)
    return SimpleNamespace(agents=agents, apps=apps, owner=owner, admin=admin, editor=editor, reader=reader,
                           member=member, data=data)


def _box(owner, name, *, sensitive=False):
    runner = enrolled(Runner.objects.create(
        name=name, kind=Runner.EMDASH, host=name, owner=owner, status=Runner.ONLINE, ready=True,
        last_heartbeat_at=timezone.now(), capabilities={"data_mode": "sensitive_data"} if sensitive else {}))
    if sensitive:
        RunnerFlag.objects.create(runner=runner, flag="sensitive_data", declared_by=owner)
    return runner


def _people(world, agent) -> list[tuple]:
    """Everyone the spec's matrix names: owner, admin, a `write` editor, a `read_only` person, and a
    contact holding each one `use:<key>`. `member` is on no line: what nobody listed gets."""
    people = [("owner", world.owner), ("admin", world.admin), ("editor", world.editor),
              ("read_only", world.reader), ("unlisted", world.member)]
    return people + [(f"contact_{k}", Contact(email=f"visitor@{k}.contacts.example")) for k in _keys(agent)]


def _contexts(agent, row) -> list[dict]:
    out = [{"name": "no_turn"}, {"name": "chat", "turn": {}}, {"name": "sensitive_chat", "turn": {"sensitive": True}}]
    for site in [*_sites(agent), "site_no_agent_serves"]:
        out += [{"name": f"site:{site}", "turn": {"site": site}},
                {"name": f"sensitive_site:{site}", "turn": {"site": site, "sensitive": True}}]
    if row.get("page_resource"):
        out.append({"name": "page", "turn": {}, "resource": row["page_resource"]})
    return out


def _turn(who, context, stamp=""):
    if "turn" not in context:
        return None
    user = who if isinstance(who, get_user_model()) else None
    contact = None if user is not None else who
    turn = probe_turn(user=user, contact=contact, stamp=stamp, **context["turn"])
    if context.get("resource"):
        turn.chat_session.page_state = {"resource": context["resource"]}
    return turn


def _pks_out(value, people):
    # `person:12` depends on the order tests ran in; the address it stands for does not.
    return re.sub(r"person:(\d+)", lambda m: f"person:{people.get(int(m[1]), m[1])}", value)


def _decision(d, world) -> dict:
    by_pk = {u.pk: u.email for u in (world.owner, world.admin, world.editor, world.reader, world.member)}
    return {**dataclasses.asdict(d), "permissions": sorted(d.permissions),
            "source": _pks_out(d.source, by_pk), "basis": _pks_out(d.basis, by_pk)}


def answers(impl, world, agent, row) -> dict:
    """Everything `impl` says about one agent, as plain data."""
    keys = _keys(agent)
    stamps = ["", legacy_interface.READ, legacy_interface.ASK, *keys, RETIRED]
    requests = [None, legacy_interface.READ, legacy_interface.ASK, *keys, RETIRED]
    contexts = _contexts(agent, row)
    site_turns = {c["name"]: _turn(world.owner, c) for c in contexts if "turn" in c}
    permission_sets = [{roles.READ}, {roles.WRITE}, {roles.use("ask")}, {roles.use("read")},
                       *[{roles.use(k)} for k in keys], set()]
    return {
        "profile": {s: impl.profile(agent, s) for s in stamps},
        "envelope_profile": {f"{s}|{name}": impl.envelope_profile(agent, s, _turn(world.owner, c, s))
                             for s in stamps for c in contexts if "turn" in c for name in [c["name"]]},
        "reachable": {str(sorted(roles.ROLES.close(p))): impl.reachable(roles.ROLES.close(p), agent.interface)
                      for p in permission_sets},
        "page_sites": sorted(impl.page_sites(agent)),
        "sensitive_capabilities": impl.sensitive_capabilities(agent.interface),
        "handles_sensitive": {s: impl.handles_sensitive(agent, s) for s in stamps},
        "as_claim": {s: impl.as_claim(agent, s) for s in stamps},
        "facts_for": {s: impl.facts_for(probe_turn(stamp=s), agent, None, [rr.SENSITIVE_DATA]) for s in stamps},
        "page_turn": {name: impl.page_turn(t, agent) for name, t in site_turns.items()},
        "page_site_of": {name: impl.page_site_of(t, agent) for name, t in site_turns.items()},
        "site": {s: {"ceiling": impl.site_ceiling(agent, s), "own_login": impl.own_login_of(agent, s),
                     "capabilities": impl.site_capabilities(agent, s)}
                 for s in [*_sites(agent), "site_no_agent_serves"]},
        "agent_entry": {name: impl.agent_entry(app, agent)
                        for (ws, name), app in sorted(world.apps.items()) if ws == agent.workspace_id},
        "decide": {
            label: {
                c["name"]: {
                    str(requested): _decision(impl.decide(agent, who, verified=isinstance(who, get_user_model()),
                                                    capability=requested, turn=_turn(who, c)), world)
                    for requested in requests}
                for c in contexts}
            for label, who in _people(world, agent)},
    }


def _differences(left, right, path="") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        return [d for k in sorted(set(left) | set(right), key=str)
                for d in _differences(left.get(k, "<absent>"), right.get(k, "<absent>"), f"{path}/{k}")]
    return [] if left == right else [f"{path}: real {left!r} != frozen {right!r}"]


#: Words, not outcomes. A refusal's `reason` is `apps/agents/refusals.py`'s (Turn Context Stage 4). A profile's
#: `description`, and the version that marks the default read profile's text, were reworded by Stage 5 (read mode
#: is never called "Ask"); every field that says what a profile may DO (tools, bash, paths, entry, data) is still
#: compared whole, so a widened profile still fails here.
_WORDS = {"reason": bool, "description": lambda _v: "<words>", "default_profile_version": lambda _v: "<version>"}


def _outcomes(doc):
    """Who gets what, not how it is worded: whether a refusal has a reason, never what it says; a profile's
    description and text version, never their words."""
    if isinstance(doc, dict):
        return {k: (_WORDS[k](v) if k in _WORDS else _outcomes(v)) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_outcomes(v) for v in doc]
    return doc


@override_settings(CANOPY_PUBLIC_BASE_URL=BASE_URL)
def test_the_real_code_answers_as_the_frozen_copy_does(world):
    wrong = []
    for agent, row in world.agents:
        wrong += [f"{agent.slug}{d}" for d in _differences(_outcomes(answers(REAL, world, agent, row)),
                                                           _outcomes(answers(legacy_interface, world, agent, row)))]
    assert not wrong, "\n".join(wrong[:30])


@override_settings(CANOPY_PUBLIC_BASE_URL=BASE_URL)
def test_the_frozen_copy_has_not_moved(world):
    everything = {agent.slug: answers(legacy_interface, world, agent, row) for agent, row in world.agents}
    canonical = json.dumps(everything, sort_keys=True, separators=(",", ":"), default=str)
    assert hashlib.sha256(canonical.encode()).hexdigest() == FROZEN_ANSWERS, (
        "tests/legacy_interface.py or its fixture now answers differently from when it was frozen. "
        "It is the reference for moving off Agent.interface: change the code under test instead.")


def test_the_fixture_has_the_shape_the_spec_counts_on(world):
    """The spec's own counts, so a re-capture that changes them is noticed, not absorbed."""
    live = [row["interface"].get("capabilities") or {} for row in world.data["agents"]]
    assert len(live) == 18
    assert sum(not caps for caps in live) == 11
    assert sum(any(c.get("sites") for c in caps.values()) for caps in live) == 3
    connections = [(name, site) for caps in live for name, c in caps.items() for site in c.get("sites") or []]
    assert sorted(connections) == [("canopy", "canopy-web"), ("canopy", "canopy-web"),
                                   ("connect", "connect-labs"), ("data", "connect-labs")]
    assert sum("ask" in caps or "read" in caps for caps in live) == 6
    assert not [c for caps in live for c in caps.values() if c.get("pages")]


def test_a_saved_turn_on_a_real_session_gets_the_same_answers(world):
    """The same comparison through what a claim reads: a saved session and turn, `turn_context.for_turn`,
    the envelope's profile and standing, and the statement for it."""
    ace = next(agent for agent, _row in world.agents if agent.slug == "ace")
    labsdata = next(agent for agent, _row in world.agents if agent.slug == "labsdata")
    for agent, site, sensitive in ((ace, "connect-labs", False), (labsdata, "connect-labs", True)):
        meta = {"embed_app": site, **({rr.METADATA_KEY: [rr.SENSITIVE_DATA]} if sensitive else {})}
        session = Session.objects.create(agent=agent, workspace=agent.workspace, created_by=world.member, metadata=meta)
        for person in (world.owner, world.admin, world.editor, world.reader):
            turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                                       initiator_kind="user", initiator_user=person, initiator_assurance="session",
                                       prompt="hello", idempotency_key=f"{agent.slug}-{person.pk}")
            real = _as_decision(turn_context.for_turn(turn, agent))
            frozen = legacy_interface.decide(agent, person, verified=True, turn=turn)
            assert _outcomes(dataclasses.asdict(real)) == _outcomes(dataclasses.asdict(frozen))
            turn.capability = real.stamp or ""
            turn.save(update_fields=["capability"])
            facts = interface.grant_facts(turn, agent)
            assert facts["granted_by"]
            assert _envelope_profile(agent, turn.capability, turn) == \
                legacy_interface.envelope_profile(agent, turn.capability, turn)
            assert interface.page_turn(turn, agent) == legacy_interface.page_turn(turn, agent)
            assert execution.facts_for(turn, agent, session, [rr.SENSITIVE_DATA]) == \
                legacy_interface.facts_for(turn, agent, session, [rr.SENSITIVE_DATA])
    assert grants.lines_for(ace), "the matrix is empty if the lines did not save"
