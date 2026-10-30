"""The agent role ladder and the access list — the foundation (nothing reads it yet).

`docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md`: a role is what a
person may do with ONE agent, and the access list says who has which role as `(who, role)`
lines, where `who` can be a group. The strongest matching line wins.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from apps.agents import grants, roles
from apps.agents.grants import Asker
from apps.agents.models import Agent, AgentGrant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
G = AgentGrant


# --- the ladder ----------------------------------------------------------------------

def test_the_ladder_is_strongest_first_and_each_role_includes_the_one_below():
    assert roles.LADDER == (roles.OWNER, roles.ADMIN, roles.EDITOR, roles.USER, roles.VIEWER)
    ranks = [roles.rank(r) for r in roles.LADDER]
    assert ranks == sorted(ranks, reverse=True)
    for upper, lower in zip(roles.LADDER, roles.LADDER[1:]):
        for permission in roles.PERMISSIONS:
            if roles.allows(lower, permission):
                assert roles.allows(upper, permission), (upper, lower, permission)


@pytest.mark.parametrize("role,can,cannot", [
    (roles.VIEWER, {roles.READ}, {roles.WRITE, roles.RUN, roles.EDIT, roles.ADMINISTER, roles.OWN}),
    (roles.USER, {roles.READ, roles.WRITE}, {roles.RUN, roles.EDIT, roles.ADMINISTER, roles.OWN}),
    (roles.EDITOR, {roles.READ, roles.WRITE, roles.RUN, roles.EDIT}, {roles.ADMINISTER, roles.OWN}),
    (roles.ADMIN, {roles.READ, roles.WRITE, roles.RUN, roles.EDIT, roles.ADMINISTER}, {roles.OWN}),
    (roles.OWNER, roles.PERMISSIONS, set()),
])
def test_what_each_role_may_do(role, can, cannot):
    assert all(roles.allows(role, p) for p in can)
    assert not any(roles.allows(role, p) for p in cannot)


def test_no_role_may_do_nothing_and_a_typo_is_loud():
    assert not any(roles.allows(None, p) for p in roles.PERMISSIONS)
    assert not roles.allows("member", roles.READ)  # the retired word grants nothing
    with pytest.raises(ValueError):
        roles.allows(roles.USER, "wirte")


# --- the access list -----------------------------------------------------------------

@pytest.fixture()
def w():
    mk = get_user_model().objects.create_user
    boss, ed, vw, ada, out = (mk(n, f"{n}@dimagi.com", "pw") for n in ("boss", "ed", "vw", "ada", "out"))
    root = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=boss)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=boss, parent=root)
    other = Workspace.objects.create(slug="ops", display_name="Ops", created_by=boss, parent=root)
    M.objects.create(user=boss, workspace=root, role=M.OWNER)
    M.objects.create(user=ed, workspace=root, role=M.EDITOR)       # an editor of the ROOT only
    M.objects.create(user=ed, workspace=connect, role=M.EDITOR)
    M.objects.create(user=vw, workspace=root, role=M.VIEWER)
    M.objects.create(user=ada, workspace=connect, role=M.ADMIN)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=boss)
    return {"root": root, "connect": connect, "ops": other, "hal": hal, "boss": boss, "ed": ed,
            "vw": vw, "ada": ada, "out": out}


def _asker(user=None, *, email=None, verified=True):
    return Asker(user=user, email=email if email is not None else getattr(user, "email", ""),
                 verified=verified)


def _line(scope, role, **kw):
    kw.setdefault("kind", G.PERSON)
    return G.objects.create(**({"agent": scope} if isinstance(scope, Agent) else {"workspace": scope}),
                            role=role, **kw)


def test_nothing_on_the_list_means_no_role(w):
    assert grants.resolve(w["hal"], _asker(w["ed"])).role is None


def test_a_named_person_gets_their_role(w):
    _line(w["hal"], roles.USER, user=w["out"])
    got = grants.resolve(w["hal"], _asker(w["out"]))
    assert (got.role, got.basis) == (roles.USER, f"grant:person:{w['out'].pk}")
    assert grants.resolve(w["hal"], _asker(w["vw"])).role is None


def test_everyone_in_a_workspace_is_a_group_line_and_at_least_narrows_it(w):
    _line(w["hal"], roles.USER, kind=G.WORKSPACE, subject_workspace=w["root"])
    _line(w["hal"], roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["root"], at_least=M.EDITOR)
    assert grants.resolve(w["hal"], _asker(w["vw"])).role == roles.USER       # any member of dimagi
    assert grants.resolve(w["hal"], _asker(w["ed"])).role == roles.EDITOR     # editors of dimagi
    assert grants.resolve(w["hal"], _asker(w["out"])).role is None            # not in dimagi at all


def test_a_parents_owner_counts_in_a_child_workspace_group(w):
    # The boss only owns the ROOT; the workspace layer already makes them an owner of connect.
    _line(w["hal"], roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["connect"], at_least=M.ADMIN)
    assert grants.resolve(w["hal"], _asker(w["boss"])).role == roles.EDITOR
    assert grants.resolve(w["hal"], _asker(w["ada"])).role == roles.EDITOR
    assert grants.resolve(w["hal"], _asker(w["ed"])).role is None             # an editor is below the floor


def test_a_domain_line_matches_by_address_member_or_not_and_needs_proof_by_default(w):
    _line(w["hal"], roles.VIEWER, kind=G.DOMAIN, domain="Dimagi.com")
    # A canopy member AND a stranger by address alike: the address is what is asked.
    assert grants.resolve(w["hal"], _asker(w["ed"])).role == roles.VIEWER
    assert grants.resolve(w["hal"], _asker(email="new@dimagi.com")).role == roles.VIEWER
    assert grants.resolve(w["hal"], _asker(email="new@dimagi.com", verified=False)).role is None
    # Exactly that domain: not a suffix, not a subdomain.
    assert grants.resolve(w["hal"], _asker(email="x@dimagi.com.evil.org")).role is None
    assert grants.resolve(w["hal"], _asker(email="x@mail.dimagi.com")).role is None
    assert grants.resolve(w["hal"], _asker()).role is None                    # nobody established who


def test_an_unverified_line_admits_an_unproven_sender_and_a_blank_domain_any_address(w):
    _line(w["hal"], roles.VIEWER, kind=G.DOMAIN, domain="", verified=False)
    assert grants.resolve(w["hal"], _asker(email="x@anywhere.org", verified=False)).role == roles.VIEWER
    assert grants.resolve(w["hal"], _asker(email="", verified=False)).role is None


def test_the_strongest_matching_line_wins_and_all_matches_are_listed(w):
    _line(w["hal"], roles.VIEWER, kind=G.DOMAIN, domain="dimagi.com")
    _line(w["hal"], roles.USER, kind=G.WORKSPACE, subject_workspace=w["root"])
    _line(w["hal"], roles.EDITOR, user=w["ed"])
    got = grants.resolve(w["hal"], _asker(w["ed"]))
    assert got.role == roles.EDITOR
    assert [ln.role for ln in got.lines] == [roles.EDITOR, roles.USER, roles.VIEWER]


def test_manual_only_holds_only_while_every_line_at_the_top_role_demands_it(w):
    _line(w["hal"], roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["connect"], at_least=M.EDITOR,
          manual_only=True)
    assert grants.resolve(w["hal"], _asker(w["ed"])).manual_only is True
    _line(w["hal"], roles.EDITOR, user=w["ed"])                                # a named, ungated editor line
    assert grants.resolve(w["hal"], _asker(w["ed"])).manual_only is False


# --- defaults from the workspace tree -------------------------------------------------

def test_a_workspace_default_reaches_every_agent_in_it_and_below(w):
    _line(w["root"], roles.USER, kind=G.WORKSPACE, subject_workspace=w["root"])
    sift = Agent.objects.create(slug="sift", name="Sift", workspace=w["ops"], owner=w["boss"])
    for agent in (w["hal"], sift):                                            # connect and ops are both below dimagi
        assert grants.resolve(agent, _asker(w["vw"])).role == roles.USER


def test_a_default_set_on_a_child_does_not_reach_its_siblings_or_parent(w):
    _line(w["connect"], roles.USER, kind=G.WORKSPACE, subject_workspace=w["root"])
    sift = Agent.objects.create(slug="sift", name="Sift", workspace=w["ops"], owner=w["boss"])
    root_agent = Agent.objects.create(slug="eva", name="Eva", workspace=w["root"], owner=w["boss"])
    assert grants.resolve(w["hal"], _asker(w["vw"])).role == roles.USER
    assert grants.resolve(sift, _asker(w["vw"])).role is None
    assert grants.resolve(root_agent, _asker(w["vw"])).role is None


def test_an_agent_can_opt_out_of_its_workspaces_defaults_and_keep_its_own_lines(w):
    _line(w["root"], roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["root"], at_least=M.EDITOR)
    assert grants.resolve(w["hal"], _asker(w["ed"])).role == roles.EDITOR
    Agent.objects.filter(pk=w["hal"].pk).update(inherit_workspace_defaults=False)
    w["hal"].refresh_from_db()
    assert grants.resolve(w["hal"], _asker(w["ed"])).role is None
    _line(w["hal"], roles.USER, user=w["ed"])
    assert grants.resolve(w["hal"], _asker(w["ed"])).role == roles.USER


def test_a_new_agent_inherits_defaults_and_nothing_else(w):
    assert w["hal"].inherit_workspace_defaults is True
    assert grants.lines_for(w["hal"]) == []


def test_bulk_read_workspace_roles_answer_the_same_as_reading_them_per_workspace(w):
    from apps.workspaces import services as wsvc

    _line(w["hal"], roles.USER, kind=G.WORKSPACE, subject_workspace=w["root"])
    lines = grants.lines_for(w["hal"])
    for user in (w["boss"], w["ed"], w["vw"], w["ada"], w["out"]):
        bulk = Asker(user=user, email=user.email, verified=True,
                     workspace_roles=wsvc.member_roles_of([user])[user.pk])
        assert grants.resolve(w["hal"], bulk, lines) == grants.resolve(w["hal"], _asker(user), lines)


# --- the table's own integrity --------------------------------------------------------

def test_a_line_sits_on_an_agent_or_a_workspace_never_both_or_neither(w):
    with pytest.raises(IntegrityError), transaction.atomic():
        G.objects.create(agent=w["hal"], workspace=w["root"], kind=G.PERSON, user=w["ed"], role=roles.USER)
    with pytest.raises(IntegrityError), transaction.atomic():
        G.objects.create(kind=G.PERSON, user=w["ed"], role=roles.USER)


def test_one_line_per_subject_per_scope(w):
    _line(w["hal"], roles.USER, user=w["ed"])
    with pytest.raises(IntegrityError), transaction.atomic():
        _line(w["hal"], roles.EDITOR, user=w["ed"])
    _line(w["root"], roles.USER, user=w["ed"])                                # same subject, another scope: fine
    _line(w["hal"], roles.USER, kind=G.DOMAIN, domain="dimagi.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        _line(w["hal"], roles.VIEWER, kind=G.DOMAIN, domain="DIMAGI.COM")     # the domain is folded to lower case
