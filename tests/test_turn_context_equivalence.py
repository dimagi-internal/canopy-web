"""The resolver answers exactly as `access.decide` did, for everyone, everywhere.

The Turn Context changes no one's access, so
this file is the proof. It first ran green against the real `decide`, in the commit before that was
deleted; `tests/legacy_decide.py` is that code, unchanged. It walks every combination `tests/test_access_matrix.py` and
`tests/fixtures/answering_interfaces.json` name: relationship, role, verified, named host site, site,
page, sensitive, review chat, stamped turns, retired names, agentless. Same access, same stamp, same
permissions, same role, source and basis, and a reason exactly when there is a refusal. The reason's words are
compared by nobody: they are `apps/agents/refusals.py`'s, and Stage 4 rewrote them on purpose.

A difference means someone's access would change. The fix is in the resolver, never here.

One change was made on purpose: on 2026-11-06 the owner removed the `session_writer` rule, in the resolver
and in the reference alike. `test_being_added_to_an_admins_session_no_longer_raises_access` names the cases
that changed.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from apps.agents import answering_snapshot, grants, published_profiles, roles, session_context, turn_context
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.agents.standings import Membership, Standings
from apps.canopy_sessions.models import REVIEW_METADATA_KEY, Session, SessionParticipant
from apps.contacts.models import Contact
from apps.harness import caller_context
from apps.harness import initiator as who_mod
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

from . import legacy_decide as reference  # the rule that was replaced, frozen as it was deleted
from .access_helpers import allow, declare, editors_run

pytestmark = pytest.mark.django_db

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "answering_interfaces.json"
RETIRED = "retired_probe"
#: `test_the_walk_has_not_shrunk`.
WALK = 49808
M = WorkspaceMembership
HAL = {"ask": {"description": "Ask", "tools": ["Read"]}}
TWO_PROFILES = {"ask": {"description": "Ask", "tools": ["Read"]}, "status": {"description": "Status", "tools": ["Read"]}}
SITE = "connect-labs"
#: One agent with a site entry and a sensitive entry, so narrowing has something to find.
EVERYTHING = {
    "ask": {"tools": ["Read"]},
    "connect": {"tools": ["Read"], "sites": [SITE]},
    "data": {"tools": ["Read"], "sites": ["data-site"], "data": "sensitive"},
}
#: What a page says it is showing. It selects no entry: every conversation on it answers as off it.
PAGE = "stock://item/1"


def _outcome(answer: dict) -> dict:
    # The outcome, not its wording: whether there is a reason, never what it says.
    return {**answer, "reason": bool(answer["reason"])}


def _old_answer(d) -> dict:
    return _outcome({**dataclasses.asdict(d), "permissions": sorted(d.permissions), "stamp": d.stamp})


def _new_answer(ctx) -> dict:
    return _outcome(answering_snapshot.answer_of(ctx))


@pytest.fixture()
def people():
    make = lambda name: get_user_model().objects.create_user(name, f"{name}@dimagi.com", "pw")  # noqa: E731
    return SimpleNamespace(
        boss=make("boss"), owner=make("owner"), admin=make("admin"), ws_admin=make("wsadmin"),
        editor=make("editor"), viewer=make("viewer"), outsider=make("outsider"), own_login=make("ownlogin"))


def _workspace(slug, p):
    ws, made = Workspace.objects.get_or_create(slug=slug, defaults={"display_name": slug, "created_by": p.boss})
    if made:
        for user, role in ((p.boss, M.OWNER), (p.owner, M.EDITOR), (p.admin, M.EDITOR), (p.ws_admin, M.ADMIN),
                           (p.editor, M.EDITOR), (p.viewer, M.VIEWER)):
            M.objects.create(user=user, workspace=ws, role=role)
    return ws


def _agent(slug, ws, p, iface=None, **extra):
    agent = Agent.objects.create(slug=slug, name=slug, workspace=ws, owner=p.owner, turn_mode="auto",
                                 interface=iface or {}, **extra)
    AgentAdmin.objects.create(agent=agent, user=p.admin, granted_by=p.owner)
    return agent


def _eva(agent):
    allow(agent, role=roles.EDITOR)
    allow(agent, role=roles.EDITOR, contact=True, domain="dimagi.com", verified=True)


def _hal(agent):
    declare(agent, HAL)
    allow(agent, role=roles.READ_ONLY)
    allow(agent, role=roles.READ_ONLY, contact=True)


FIXTURE_ROWS = list({json.dumps(row["interface"], sort_keys=True): row  # an interface repeated adds no case
                     for rows in json.loads(FIXTURE.read_text()).values() if isinstance(rows, list)
                     for row in rows}.values())


def _matrix_agents(p) -> list:
    """The access list setups of `tests/test_access_matrix.py`, each person's own kind of line, and an
    agent with every kind of entry."""
    ws = _workspace("connect", p)
    out = []
    for name, setup, editors in (("none", None, False), ("editors", None, True), ("eva", _eva, True), ("hal", _hal, True),
                                 ("eva-confined", _eva, False), ("hal-confined", _hal, False)):
        agent = _agent(f"m-{name}", ws, p)
        declare(agent, None)
        if setup:
            setup(agent)
        if editors:
            editors_run(agent)
        out.append(agent)
    lines = {"read-only": {"role": roles.READ_ONLY}, "read-write": {"role": roles.READ_WRITE},
             "editor": {"role": roles.EDITOR}, "use-read-mode": {"capability": "ask"}, "use-status": {"capability": "status"},
             "manual": {"role": roles.READ_WRITE, "manual_only": True}}
    for name, line in lines.items():
        agent = _agent(f"l-{name}", ws, p)
        declare(agent, TWO_PROFILES)
        AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=p.viewer, **line)
        out.append(agent)
    # Written as stored, not through `parse`.
    agent = _agent("all-kinds", ws, p, {"capabilities": EVERYTHING}, user=p.own_login)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=p.viewer, role=roles.READ_ONLY)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=p.editor, role=roles.EDITOR)
    # One person with a site's entry and a line naming an entry that is gone.
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=p.ws_admin, capability="connect")
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=p.ws_admin, capability=RETIRED)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.CONTACT, capability="data", domain="partner.org", verified=False)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.CONTACT, capability="connect", domain="dimagi.com", verified=True)
    out.append(agent)
    return out


def _fixture_agents(p, only=None) -> list:
    """Every agent shape in the fixture, with a `write` line, a `read` line and a contact per entry."""
    out = []
    for row in FIXTURE_ROWS:
        if only is not None and row["slug"] != only:
            continue
        ws = _workspace(row["workspace"], p)
        agent = _agent(row["slug"], ws, p, row["interface"], email=row.get("email", ""))
        AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=p.editor, role=roles.EDITOR)
        AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=p.viewer, role=roles.READ_ONLY)
        for key in (row["interface"].get("capabilities") or {}):
            AgentGrant.objects.create(agent=agent, kind=AgentGrant.CONTACT, capability=key,
                                      domain=f"{key}.contacts.example", verified=False)
        out.append(agent)
    return out


def _keys(agent) -> list[str]:
    return list((agent.interface or {}).get("capabilities") or {})


def _requested(agent) -> list:
    return [None, published_profiles.READ_MODE_NAME, published_profiles.READ_MODE_STAMP, *_keys(agent), RETIRED,
            published_profiles.SESSION_REVIEW]


def _askers(agent, p) -> list[SimpleNamespace]:
    """Every relationship, proven and not: the fields a turn from them would carry, and who `decide` is
    handed with no turn."""
    user = lambda u, assurance=who_mod.SESSION: SimpleNamespace(  # noqa: E731
        who=u, verified=bool(assurance), kind=who_mod.USER, fields={"initiator_user": u, "initiator_assurance": assurance})
    contact = lambda email, grade="": SimpleNamespace(  # noqa: E731
        who=Contact(email=email), verified=bool(grade), kind=who_mod.CONTACT,
        fields={"initiator_contact": Contact(email=email), "initiator_assurance": grade})
    out = [user(u) for u in (p.owner, p.boss, p.admin, p.ws_admin, p.editor, p.viewer, p.outsider, p.own_login)]
    # The same people, on a message that does not prove it.
    out += [user(u, "") for u in (p.owner, p.admin, p.editor, p.viewer)]
    out += [contact("staff@dimagi.com", Contact.AUTH_DMARC), contact("staff@dimagi.com"),
            contact("someone@partner.org", Contact.AUTH_DMARC), contact("someone@partner.org")]
    out += [contact(f"visitor@{k}.contacts.example") for k in _keys(agent)[:3]]
    # What a caller says and what the message proves can differ, as the snapshot's probes do.
    out += [SimpleNamespace(**{**vars(user(p.owner, "")), "verified": True}),
            SimpleNamespace(**{**vars(contact("staff@dimagi.com", Contact.AUTH_DMARC)), "verified": False})]
    # A contact's turn that also names a person: the rule reads that person only on a user's turn.
    out += [SimpleNamespace(**{**vars(contact("staff@dimagi.com", Contact.AUTH_DMARC)),
                               "fields": {"initiator_contact": Contact(email="staff@dimagi.com"), "initiator_user": p.viewer,
                                          "initiator_assurance": Contact.AUTH_DMARC}})]
    # Canopy itself, another agent, canopy on someone's behalf, and nobody at all.
    out += [SimpleNamespace(who=None, verified=True, kind=who_mod.SYSTEM, fields={}),
            SimpleNamespace(who=None, verified=True, kind=who_mod.AGENT, fields={"initiator_agent": "other"}),
            SimpleNamespace(who=p.viewer, verified=True, kind=who_mod.SYSTEM, fields={"initiator_user": p.viewer}),
            SimpleNamespace(who=None, verified=False, kind="", fields={})]
    return out


def _conversations(agent) -> list[dict]:
    """No conversation, one with no site, one on each site, one on a page, and a review chat; each of
    those ordinary and sensitive. No entry selects by page, so a page's resource changes nothing."""
    sites = sorted(published_profiles.page_sites(agent))
    out = [{"no_turn": True}, {}, {"sensitive": True}, {"review": True}, {"review": True, "sensitive": True}]
    for site in [*sites, "site_no_agent_serves"]:
        out += [{"site": site}, {"site": site, "sensitive": True}, {"site": site, "review": True}]
    out += [{"page": PAGE}, {"page": PAGE, "sensitive": True}]
    out += [{"page": PAGE, "site": site} for site in sites]
    return out


def _probe(asker, conv) -> Turn | None:
    if conv.get("no_turn"):
        return None
    metadata = {}
    if conv.get("site"):
        metadata["embed_app"] = conv["site"]
    if conv.get("sensitive"):
        metadata[rr.METADATA_KEY] = [rr.SENSITIVE_DATA]
    if conv.get("review"):
        metadata[REVIEW_METADATA_KEY] = "00000000-0000-0000-0000-000000000009"
    session = Session(pk=answering_snapshot.PROBE_SESSION, metadata=metadata,
                      page_state={"resource": conv["page"]} if conv.get("page") else {})
    return Turn(pk=answering_snapshot.PROBE_TURN, chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                initiator_kind=asker.kind, **asker.fields)


def _cases(agent, p):
    """`(asker, conversation, requested, turn-free?)` for one agent."""
    for asker in _askers(agent, p):
        for conv in _conversations(agent):
            for requested in _requested(agent):
                yield asker, conv, requested


def _name(asker) -> str:
    return getattr(asker.who, "email", "") or asker.kind or "nobody"


def _differs(old: dict, new: dict) -> dict:
    return {k: (old[k], new[k]) for k in old if old[k] != new[k]}


def _compare(agent, p) -> tuple[int, list[str]]:
    wrong, count = [], 0
    lines = grants.lines_for(agent)
    users = [a.who for a in _askers(agent, p) if isinstance(a.who, get_user_model())]
    standings = Standings({u.pk: u for u in users}.values(), [agent])
    for asker, conv, requested in _cases(agent, p):
        turn = _probe(asker, conv)
        # With no turn, three ways: reading for itself, handed the access list, handed everything in bulk.
        ways = [{}]
        if turn is None:
            ways.append({"lines": lines})
            if isinstance(asker.who, get_user_model()):
                ways.append({"standing": standings.of(asker.who, agent), "lines": lines})
        for way in ways:
            old = _old_answer(reference.decide(agent, asker.who, verified=asker.verified, capability=requested, turn=turn, **way))
            new = _new_answer(turn_context.for_asker(agent, asker.who, verified=asker.verified, requested=requested,
                                                     turn=turn, **way))
            count += 1
            if old != new:
                wrong.append(f"{agent.slug} {_name(asker)} {conv} requested={requested} {sorted(way)}: {_differs(old, new)}")
        if turn is not None:
            old = _old_answer(reference.decide_for_turn(turn, agent, requested))
            new = _new_answer(turn_context.for_turn(turn, agent, requested))
            count += 1
            if old != new:
                wrong.append(f"for_turn {agent.slug} {_name(asker)} {conv} requested={requested}: {_differs(old, new)}")
    return count, wrong


# --- one test per agent, so the walk spreads over CI's workers ------------------------------------

MATRIX_SLUGS = ["m-none", "m-editors", "m-eva", "m-hal", "m-eva-confined", "m-hal-confined", "l-read-only",
                "l-read-write", "l-editor", "l-use-read-mode", "l-use-status", "l-manual", "all-kinds"]
FIXTURE_SLUGS = [row["slug"] for row in FIXTURE_ROWS]


@pytest.mark.parametrize("slug", MATRIX_SLUGS)
def test_every_case_of_the_access_matrix(people, slug):
    agent = next(a for a in _matrix_agents(people) if a.slug == slug)
    count, wrong = _compare(agent, people)
    assert not wrong, f"{len(wrong)} of {count} differ:\n" + "\n".join(wrong[:30])


@pytest.mark.parametrize("slug", FIXTURE_SLUGS)
def test_every_agent_shape_in_the_fixture(people, slug):
    agent = _fixture_agents(people, only=slug)[0]
    count, wrong = _compare(agent, people)
    assert not wrong, f"{len(wrong)} of {count} differ:\n" + "\n".join(wrong[:30])


def test_the_walk_has_not_shrunk(people):
    """The number of cases, so that an edit that drops a dimension is seen rather than passing quietly."""
    agents = [*_matrix_agents(people), *_fixture_agents(people)]
    assert {a.slug for a in agents} == {*MATRIX_SLUGS, *FIXTURE_SLUGS}
    assert len(FIXTURE_SLUGS) == 14   # 18 live agents and 6 corner cases: 11 of them hold the same empty interface
    assert sum(1 for a in agents for _case in _cases(a, people)) == WALK


# --- a saved session: being in it, and what only a real row knows --------------------------------

def _chat(agent, creator, *, role=None, user=None, sensitive=False):
    session = Session.objects.create(agent=agent, workspace=agent.workspace, created_by=creator,
                                     metadata={rr.METADATA_KEY: [rr.SENSITIVE_DATA]} if sensitive else {})
    if role:
        SessionParticipant.objects.create(session=session, user=user, role=role)
    return session


def _turn_in(session, user, key, **extra) -> Turn:
    return Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, initiator_kind=who_mod.USER,
                               initiator_user=user, initiator_assurance=who_mod.SESSION, prompt="hi",
                               idempotency_key=key, **extra)


def _same(turn, agent, requested=None):
    old, new = (_old_answer(reference.decide_for_turn(turn, agent, requested)),
                _new_answer(turn_context.for_turn(turn, agent, requested)))
    assert old == new, _differs(old, new)
    return new


@pytest.mark.parametrize("creator", ["owner", "admin", "viewer", "nobody"])
@pytest.mark.parametrize("role", [SessionParticipant.OWNER, SessionParticipant.EDITOR, SessionParticipant.VIEWER, None])
@pytest.mark.parametrize("sensitive", [False, True])
def test_being_in_a_session_gives_nobody_more_than_their_own_line(people, creator, role, sensitive):
    """Whoever started it and whatever their row, a participant answers as they would with no session."""
    agent = _matrix_agents(people)[-1]
    session = _chat(agent, getattr(people, creator, None), role=role, user=people.outsider, sensitive=sensitive)
    for requested in (None, "connect", "data"):
        new = _same(_turn_in(session, people.outsider, f"in-{requested}"), agent, requested)
        assert new["access"] == "none" and not new["basis"].startswith("session")   # the outsider holds no line
        _same(_turn_in(session, people.viewer, f"in-someone-else-{requested}"), agent, requested)
    assert session_context.build(session).session_grants == ()


#: The cases the removal changed: an editor or owner participant in a session the agent's owner or an admin
#: started. Before 2026-11-06 every one of them was `full` (basis `session:<role>`); now each answers by their
#: own line. `(person, sensitive, requested) -> (access, stamp, basis, refusal code)`.
CHANGED = {
    ("outsider", False, None): ("none", None, "refused", "not_on_access_list"),
    ("outsider", False, "data"): ("none", None, "refused", "not_on_access_list"),
    ("outsider", True, "data"): ("none", None, "refused", "not_on_access_list"),
    ("viewer", False, None): ("confined", "ask", "capability:ask", None),
    ("viewer", True, "data"): ("none", None, "refused", "profile_not_open"),
}
#: Unchanged: a `write` line was already the whole agent, and stays it.
UNCHANGED = {
    ("editor", False, None): ("full", "", "grant:", None),   # `grant:<their line>`
    ("editor", True, "data"): ("confined", "data", "sensitive:data", None),
}


@pytest.mark.parametrize("creator", ["owner", "admin"])
@pytest.mark.parametrize("role", [SessionParticipant.OWNER, SessionParticipant.EDITOR])
def test_being_added_to_an_admins_session_no_longer_raises_access(people, creator, role):
    """The `session_writer` rule's removal, case by case: no line is refused as anyone with no line is, a read
    line gives read mode, a write line is the whole agent as before, and a sensitive session holds each to its
    own line too. The fix for someone who lost access is a line on the access list."""
    agent = _matrix_agents(people)[-1]
    for (name, sensitive, requested), want in {**CHANGED, **UNCHANGED}.items():
        person = getattr(people, name)
        session = _chat(agent, getattr(people, creator), role=role, user=person, sensitive=sensitive)
        new = _same(_turn_in(session, person, f"{name}-{sensitive}-{requested}"), agent, requested)
        ctx = turn_context.for_turn(_turn_in(session, person, f"ctx-{name}-{sensitive}-{requested}"), agent, requested)
        basis = "grant:" if new["basis"].startswith("grant:") else new["basis"]
        got = (new["access"], new["stamp"], basis, ctx.refusal.code if ctx.refused else None)
        assert got == want, (name, sensitive, requested)
        assert ctx.standing.source != "session"
        assert [e.outcome for e in ctx.trace if e.rule == "session_grants"] in ([], ["none"])


def test_a_session_is_read_from_the_chat_never_from_where_a_turn_ran(people):
    agent = _matrix_agents(people)[-1]
    session = _chat(agent, people.owner, role=SessionParticipant.EDITOR, user=people.outsider)
    turn = Turn.objects.create(agent=agent, canopy_session=session, origin=Turn.ORIGIN_API, initiator_kind=who_mod.USER,
                               initiator_user=people.outsider, initiator_assurance=who_mod.SESSION, prompt="hi",
                               idempotency_key="k")
    assert _same(turn, agent)["access"] == "none"


def test_a_turn_with_no_session_inherits_sensitivity_and_a_review_from_where_it_came(people):
    agent = _matrix_agents(people)[-1]
    parent = _chat(agent, people.owner, sensitive=True)
    child = Turn.objects.create(agent=agent, parent_session=parent, origin=Turn.ORIGIN_API, initiator_kind=who_mod.USER,
                                initiator_user=people.owner, initiator_assurance=who_mod.SESSION, prompt="hi",
                                idempotency_key="dispatched")
    assert _same(child, agent)["access"] == "none"
    review = Session.objects.create(agent=agent, workspace=agent.workspace, created_by=people.owner,
                                    metadata={REVIEW_METADATA_KEY: "x"})
    named = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, initiator_kind=who_mod.USER, initiator_user=people.owner,
                                initiator_assurance=who_mod.SESSION, prompt="hi", idempotency_key="named",
                                origin_ref={"chat_session_id": str(review.pk)})
    assert _same(named, agent)["capability"] == published_profiles.SESSION_REVIEW


def test_a_stale_standing_handed_in_with_a_turn_answers_as_it_did(people):
    # The relationship is read fresh from the turn, whether they are an admin from what was handed in.
    agent = _matrix_agents(people)[-1]
    session = _chat(agent, people.owner)
    for user in (people.owner, people.admin):
        turn = _turn_in(session, user, f"stale-{user.pk}")
        stale = Membership(None, False)
        old = _old_answer(reference.decide(agent, user, verified=True, turn=turn, standing=stale))
        new = _new_answer(turn_context.for_asker(agent, user, verified=True, turn=turn, standing=stale))
        assert old == new, _differs(old, new)


def test_acting_as_a_lower_role_is_judged_as_that_role(people):
    agent = _matrix_agents(people)[-1]
    session = _chat(agent, people.owner)
    make = lambda key: _turn_in(session, get_user_model().objects.get(pk=people.owner.pk), key,  # noqa: E731
                                origin_ref={"acts_as": "viewer"})
    assert _old_answer(reference.decide_for_turn(make("a"), agent)) == _new_answer(turn_context.for_turn(make("b"), agent))


# --- stamped turns: what the envelope reads ------------------------------------------------------

def _facts(ctx) -> dict:
    return {"granted_by": ctx.basis, "role": ctx.standing.agent_role, "source": ctx.standing.source}


@pytest.mark.parametrize("slug", ["all-kinds", "m-hal", "l-use-status", "m-none"])
def test_a_stamped_turn_keeps_its_stamp_and_reports_what_the_envelope_reports(people, slug):
    agent = next(a for a in _matrix_agents(people) if a.slug == slug)
    for asker in _askers(agent, people):
        for conv in _conversations(agent):
            if conv.get("no_turn"):
                continue
            for stamp in ["", published_profiles.READ_MODE_NAME, published_profiles.READ_MODE_STAMP, *_keys(agent), RETIRED,
                          published_profiles.SESSION_REVIEW]:
                turn = _probe(asker, conv)
                turn.capability = stamp
                session_ctx = session_context.build(None, turn, agent=agent)
                ctx = turn_context.resolve_turn_context(session_ctx, turn_context.Asker.of_turn(turn), agent, turn=turn,
                                                        stamp=stamp)
                old = reference.grant_facts(turn, agent)
                assert published_profiles.grant_facts(turn, agent) == old, (asker.who, conv, stamp)
                assert _facts(ctx) == {k: old[k] for k in ("granted_by", "role", "source")}, (asker.who, conv, stamp)
                assert ctx.stamped == stamp
                assert ctx.profile.as_envelope() == reference.envelope_profile(agent, stamp, turn), (asker.who, conv, stamp)
                assert ctx.access == (turn_context.FULL if stamp == "" else turn_context.CONFINED)


# --- no agent: `no_agent`, what the envelope already says ----------------------------------------

def test_a_turn_with_no_agent_is_what_the_envelope_says_it_is(people):
    ws = _workspace("connect", people)
    runner = Runner.objects.create(name="box", kind=Runner.EMDASH, owner=people.editor, workspace=ws)
    session = Session.objects.create(workspace=ws, created_by=people.viewer, project="repo")
    SessionParticipant.objects.create(session=session, user=people.ws_admin, role=SessionParticipant.EDITOR)
    asked = [(people.viewer, None), (people.ws_admin, None), (people.outsider, None), (people.editor, runner)]
    for i, (user, box) in enumerate(asked):
        turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, initiator_kind=who_mod.USER,
                                   initiator_user=user, initiator_assurance=who_mod.SESSION, prompt="hi",
                                   idempotency_key=f"repo-{i}", claimed_by=box)
        ctx = turn_context.for_turn(turn, None)
        grant = caller_context._grant(turn, None)
        assert ctx.access == turn_context.FULL and ctx.profile.kind == turn_context.NO_AGENT and ctx.stamped == ""
        assert ctx.standing.relationship == caller_context.relationship(turn, None)
        assert (ctx.basis, ctx.standing.agent_role, ctx.standing.source) == (grant["granted_by"], grant["role"], grant["source"])
        assert sorted(ctx.standing.permissions) == grant["permissions"]
        assert ctx.review.manual_only is grant["manual_only"]
