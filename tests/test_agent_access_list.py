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


# --- the roles ----------------------------------------------------------------------

def test_the_ladder_is_strongest_first_and_each_role_includes_the_one_below():
    assert roles.LADDER == (roles.OWNER, roles.ADMIN, roles.EDITOR, roles.READ_WRITE, roles.READ_ONLY)
    ranks = [roles.rank(r) for r in roles.LADDER]
    assert ranks == sorted(ranks, reverse=True)
    for upper, lower in zip(roles.LADDER, roles.LADDER[1:]):
        for permission in roles.PERMISSIONS:
            if roles.allows(lower, permission):
                assert roles.allows(upper, permission), (upper, lower, permission)


ALL = {roles.READ, roles.WRITE, roles.AGENT_EDIT, roles.ADMINISTER, roles.OWN}


@pytest.mark.parametrize("role,can", [
    (roles.READ_ONLY, {roles.READ}),
    (roles.READ_WRITE, {roles.READ, roles.WRITE}),
    (roles.EDITOR, {roles.READ, roles.WRITE, roles.AGENT_EDIT}),
    (roles.ADMIN, ALL - {roles.OWN}),
    (roles.OWNER, ALL),
])
def test_what_each_role_may_do(role, can):
    assert {p for p in ALL if roles.allows(role, p)} == can
    assert roles.PERMISSIONS == ALL


def test_the_dependency_rule_holds_for_every_role():
    """agent_edit implies write implies read; admin implies everything but own. Nobody holds a
    permission without the ones it depends on, whichever role they have."""
    assert roles.ROLES.close({roles.AGENT_EDIT}) >= {roles.WRITE, roles.READ}
    assert roles.ROLES.close({roles.WRITE}) >= {roles.READ}
    assert roles.ROLES.close({roles.ADMINISTER}) >= ALL - {roles.OWN}
    assert roles.OWN not in roles.ROLES.close({roles.ADMINISTER})
    for role in roles.LADDER:
        held = roles.permissions_of(role)
        if roles.AGENT_EDIT in held:
            assert roles.WRITE in held
        if roles.WRITE in held:
            assert roles.READ in held
        if roles.ADMINISTER in held:
            assert held >= ALL - {roles.OWN}


def test_write_opens_every_profile_read_opens_only_read_mode_and_a_use_line_opens_one():
    assert roles.can_use(roles.permissions_of(roles.READ_WRITE), "anything")
    assert roles.can_use(roles.permissions_of(roles.READ_ONLY), "ask")
    assert not roles.can_use(roles.permissions_of(roles.READ_ONLY), "connect")
    assert roles.can_use({roles.use("connect")}, "connect")
    assert not roles.can_use({roles.use("connect")}, "ask")      # a host-site line is not `read`
    assert not roles.can_use({roles.use("connect")}, "data")


def test_no_role_may_do_nothing_and_a_typo_is_loud():
    assert not any(roles.allows(None, p) for p in roles.PERMISSIONS)
    for retired in ("member", "viewer", "user"):
        assert not roles.allows(retired, roles.READ)  # the retired words grant nothing
    assert not hasattr(roles, "VIEWER") and not hasattr(roles, "USER")
    with pytest.raises(ValueError):
        roles.allows(roles.READ_WRITE, "wirte")


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
    _line(w["hal"], roles.READ_WRITE, user=w["out"])
    got = grants.resolve(w["hal"], _asker(w["out"]))
    assert (got.role, got.basis) == (roles.READ_WRITE, f"grant:person:{w['out'].pk}")
    assert grants.resolve(w["hal"], _asker(w["vw"])).role is None


def test_everyone_in_a_workspace_is_a_group_line_and_at_least_narrows_it(w):
    _line(w["hal"], roles.READ_WRITE, kind=G.WORKSPACE, subject_workspace=w["root"])
    _line(w["hal"], roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["root"], at_least=M.EDITOR)
    assert grants.resolve(w["hal"], _asker(w["vw"])).role == roles.READ_WRITE       # any member of dimagi
    assert grants.resolve(w["hal"], _asker(w["ed"])).role == roles.EDITOR     # editors of dimagi
    assert grants.resolve(w["hal"], _asker(w["out"])).role is None            # not in dimagi at all


def test_a_parents_owner_counts_in_a_child_workspace_group(w):
    # The boss only owns the ROOT; the workspace layer already makes them an owner of connect.
    _line(w["hal"], roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["connect"], at_least=M.ADMIN)
    assert grants.resolve(w["hal"], _asker(w["boss"])).role == roles.EDITOR
    assert grants.resolve(w["hal"], _asker(w["ada"])).role == roles.EDITOR
    assert grants.resolve(w["hal"], _asker(w["ed"])).role is None             # an editor is below the floor


def test_a_domain_line_matches_by_address_member_or_not_and_needs_proof_by_default(w):
    _line(w["hal"], roles.READ_ONLY, kind=G.DOMAIN, domain="Dimagi.com")
    # A canopy member AND a stranger by address alike: the address is what is asked.
    assert grants.resolve(w["hal"], _asker(w["ed"])).role == roles.READ_ONLY
    assert grants.resolve(w["hal"], _asker(email="new@dimagi.com")).role == roles.READ_ONLY
    assert grants.resolve(w["hal"], _asker(email="new@dimagi.com", verified=False)).role is None
    # Exactly that domain: not a suffix, not a subdomain.
    assert grants.resolve(w["hal"], _asker(email="x@dimagi.com.evil.org")).role is None
    assert grants.resolve(w["hal"], _asker(email="x@mail.dimagi.com")).role is None
    assert grants.resolve(w["hal"], _asker()).role is None                    # nobody established who


def test_an_unverified_line_admits_an_unproven_sender_and_a_blank_domain_any_address(w):
    _line(w["hal"], roles.READ_ONLY, kind=G.DOMAIN, domain="", verified=False)
    assert grants.resolve(w["hal"], _asker(email="x@anywhere.org", verified=False)).role == roles.READ_ONLY
    assert grants.resolve(w["hal"], _asker(email="", verified=False)).role is None


def test_the_strongest_matching_line_wins_and_all_matches_are_listed(w):
    _line(w["hal"], roles.READ_ONLY, kind=G.DOMAIN, domain="dimagi.com")
    _line(w["hal"], roles.READ_WRITE, kind=G.WORKSPACE, subject_workspace=w["root"])
    _line(w["hal"], roles.EDITOR, user=w["ed"])
    got = grants.resolve(w["hal"], _asker(w["ed"]))
    assert got.role == roles.EDITOR
    assert [ln.role for ln in got.lines] == [roles.EDITOR, roles.READ_WRITE, roles.READ_ONLY]


def test_manual_only_holds_only_while_every_line_that_gives_write_demands_it(w):
    _line(w["hal"], roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["connect"], at_least=M.EDITOR,
          manual_only=True)
    assert grants.resolve(w["hal"], _asker(w["ed"])).manual_only is True
    _line(w["hal"], roles.EDITOR, user=w["ed"])                                # a named, ungated editor line
    assert grants.resolve(w["hal"], _asker(w["ed"])).manual_only is False


def test_a_free_read_write_line_frees_a_gated_editor_line(w):
    """Both lines give `write`, so one without the gate frees the person (the old rule only
    compared lines of the same role; now it is the permission that matters)."""
    _line(w["hal"], roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["connect"], at_least=M.EDITOR,
          manual_only=True)
    _line(w["hal"], roles.READ_WRITE, user=w["ed"])
    assert grants.resolve(w["hal"], _asker(w["ed"])).manual_only is False
    _line(w["hal"], roles.READ_ONLY, kind=G.DOMAIN, domain="dimagi.com")      # read gives no write: not a freedom
    G.objects.filter(agent=w["hal"], user=w["ed"]).delete()
    assert grants.resolve(w["hal"], _asker(w["ed"])).manual_only is True


# --- defaults from the workspace tree -------------------------------------------------

def test_a_workspace_default_reaches_every_agent_in_it_and_below(w):
    _line(w["root"], roles.READ_WRITE, kind=G.WORKSPACE, subject_workspace=w["root"])
    sift = Agent.objects.create(slug="sift", name="Sift", workspace=w["ops"], owner=w["boss"])
    for agent in (w["hal"], sift):                                            # connect and ops are both below dimagi
        assert grants.resolve(agent, _asker(w["vw"])).role == roles.READ_WRITE


def test_a_default_set_on_a_child_does_not_reach_its_siblings_or_parent(w):
    _line(w["connect"], roles.READ_WRITE, kind=G.WORKSPACE, subject_workspace=w["root"])
    sift = Agent.objects.create(slug="sift", name="Sift", workspace=w["ops"], owner=w["boss"])
    root_agent = Agent.objects.create(slug="eva", name="Eva", workspace=w["root"], owner=w["boss"])
    assert grants.resolve(w["hal"], _asker(w["vw"])).role == roles.READ_WRITE
    assert grants.resolve(sift, _asker(w["vw"])).role is None
    assert grants.resolve(root_agent, _asker(w["vw"])).role is None


def test_an_agent_can_opt_out_of_its_workspaces_defaults_and_keep_its_own_lines(w):
    _line(w["root"], roles.EDITOR, kind=G.WORKSPACE, subject_workspace=w["root"], at_least=M.EDITOR)
    assert grants.resolve(w["hal"], _asker(w["ed"])).role == roles.EDITOR
    Agent.objects.filter(pk=w["hal"].pk).update(inherit_workspace_defaults=False)
    w["hal"].refresh_from_db()
    assert grants.resolve(w["hal"], _asker(w["ed"])).role is None
    _line(w["hal"], roles.READ_WRITE, user=w["ed"])
    assert grants.resolve(w["hal"], _asker(w["ed"])).role == roles.READ_WRITE


def test_a_new_agent_inherits_defaults_and_nothing_else(w):
    assert w["hal"].inherit_workspace_defaults is True
    assert grants.lines_for(w["hal"]) == []


def test_bulk_read_workspace_roles_answer_the_same_as_reading_them_per_workspace(w):
    from apps.workspaces import services as wsvc

    _line(w["hal"], roles.READ_WRITE, kind=G.WORKSPACE, subject_workspace=w["root"])
    lines = grants.lines_for(w["hal"])
    for user in (w["boss"], w["ed"], w["vw"], w["ada"], w["out"]):
        bulk = Asker(user=user, email=user.email, verified=True,
                     workspace_roles=wsvc.member_roles_of([user])[user.pk])
        assert grants.resolve(w["hal"], bulk, lines) == grants.resolve(w["hal"], _asker(user), lines)


# --- the table's own integrity --------------------------------------------------------

def test_a_line_sits_on_an_agent_or_a_workspace_never_both_or_neither(w):
    with pytest.raises(IntegrityError), transaction.atomic():
        G.objects.create(agent=w["hal"], workspace=w["root"], kind=G.PERSON, user=w["ed"], role=roles.READ_WRITE)
    with pytest.raises(IntegrityError), transaction.atomic():
        G.objects.create(kind=G.PERSON, user=w["ed"], role=roles.READ_WRITE)


def test_one_line_per_subject_per_scope(w):
    _line(w["hal"], roles.READ_WRITE, user=w["ed"])
    with pytest.raises(IntegrityError), transaction.atomic():
        _line(w["hal"], roles.EDITOR, user=w["ed"])
    _line(w["root"], roles.READ_WRITE, user=w["ed"])                                # same subject, another scope: fine
    _line(w["hal"], roles.READ_WRITE, kind=G.DOMAIN, domain="dimagi.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        _line(w["hal"], roles.READ_ONLY, kind=G.DOMAIN, domain="DIMAGI.COM")     # the domain is folded to lower case


# --- a line grants a role OR one named host site ---------------------------------------

def test_a_host_site_line_grants_use_of_that_site_and_nothing_else(w):
    G.objects.create(agent=w["hal"], kind=G.PERSON, user=w["out"], capability="connect")
    got = grants.resolve(w["hal"], _asker(w["out"]))
    assert got.role is None                                   # a host-site line is not a role
    assert got.permissions == {roles.use("connect")}
    assert roles.can_use(got.permissions, "connect")
    assert not roles.can_use(got.permissions, "ask") and not roles.holds(got.permissions, roles.READ)
    assert not roles.holds(got.permissions, roles.WRITE)


def test_several_host_sites_for_one_subject_are_several_lines_beside_a_role_line(w):
    G.objects.create(agent=w["hal"], kind=G.PERSON, user=w["out"], role=roles.READ_ONLY)
    G.objects.create(agent=w["hal"], kind=G.PERSON, user=w["out"], capability="connect")
    G.objects.create(agent=w["hal"], kind=G.PERSON, user=w["out"], capability="data")
    got = grants.resolve(w["hal"], _asker(w["out"]))
    assert got.role == roles.READ_ONLY
    assert got.permissions == {roles.READ, roles.use("connect"), roles.use("data")}
    assert [ln.capability for ln in got.lines] == ["", "connect", "data"]     # roles first, then by name
    with pytest.raises(IntegrityError), transaction.atomic():
        G.objects.create(agent=w["hal"], kind=G.PERSON, user=w["out"], capability="data")


def test_a_line_has_a_role_or_a_host_site_never_both_or_neither(w):
    with pytest.raises(IntegrityError), transaction.atomic():
        G.objects.create(agent=w["hal"], kind=G.PERSON, user=w["out"], role=roles.READ_ONLY, capability="x")
    with pytest.raises(IntegrityError), transaction.atomic():
        G.objects.create(agent=w["hal"], kind=G.PERSON, user=w["out"])


def test_permissions_are_the_union_of_every_matching_line(w):
    _line(w["hal"], roles.READ_ONLY, kind=G.DOMAIN, domain="dimagi.com")
    G.objects.create(agent=w["hal"], kind=G.PERSON, user=w["ed"], capability="connect")
    got = grants.resolve(w["hal"], _asker(w["ed"]))
    assert got.permissions == {roles.READ, roles.use("connect")}
    _line(w["hal"], roles.READ_WRITE, user=w["vw"])
    assert roles.holds(grants.resolve(w["hal"], _asker(w["vw"])).permissions, roles.WRITE)


# --- the safety rails, by permission -----------------------------------------------------

@pytest.mark.parametrize("kind", [G.DOMAIN, G.CONTACT])
@pytest.mark.parametrize("role", [roles.READ_WRITE, roles.EDITOR])
def test_an_address_only_line_can_never_grant_a_role_containing_write(w, kind, role):
    """An address is claimable by a message: the rail is the permission, not the role's name."""
    assert roles.allows(role, roles.WRITE)
    with pytest.raises(grants.LineError, match="drive the whole agent"):
        grants.check_line(w["boss"], home="dimagi", kind=kind, role=role, domain="dimagi.com")


@pytest.mark.parametrize("kind", [G.DOMAIN, G.CONTACT])
def test_an_address_only_line_may_give_read_only_or_a_named_host_site(w, kind):
    assert grants.check_line(w["boss"], home="dimagi", kind=kind, role=roles.READ_ONLY, domain="dimagi.com")["role"] == "read_only"
    got = grants.check_line(w["boss"], home="dimagi", kind=kind, capability="connect", domain="dimagi.com")
    assert (got["role"], got["capability"]) == ("", "connect")


def test_a_workspace_or_person_line_may_give_write(w):
    for role in (roles.READ_WRITE, roles.EDITOR):
        assert grants.check_line(w["boss"], home="dimagi", kind=G.WORKSPACE, role=role, workspace="dimagi")["role"] == role
        assert grants.check_line(w["boss"], home="dimagi", kind=G.PERSON, role=role, email=w["ed"].email)["role"] == role


@pytest.mark.parametrize("role", [roles.ADMIN, roles.OWNER, "user", "viewer", "", None])
def test_only_the_three_built_in_roles_are_addable(w, role):
    with pytest.raises(grants.LineError):
        grants.check_line(w["boss"], home="dimagi", kind=G.PERSON, role=role, email=w["ed"].email)


def test_a_line_must_name_exactly_one_of_role_and_host_site(w):
    with pytest.raises(grants.LineError, match="either a role or one host site"):
        grants.check_line(w["boss"], home="dimagi", kind=G.PERSON, role=roles.READ_ONLY, capability="x", email=w["ed"].email)
    with pytest.raises(grants.LineError, match="either a role or one host site"):
        grants.check_line(w["boss"], home="dimagi", kind=G.PERSON, email=w["ed"].email)
    with pytest.raises(grants.LineError, match="not the name of a host site"):
        grants.check_line(w["boss"], home="dimagi", kind=G.PERSON, capability="Not Valid", email=w["ed"].email)


# --- "...or any workspace below it" -------------------------------------------------------

@pytest.fixture()
def tree(w):
    """dimagi > connect, ops; and a second organisation that must never be reached."""
    mk = get_user_model().objects.create_user
    kid = mk("kid", "kid@dimagi.com", "pw")                  # only in connect, a child of dimagi
    ops_admin = mk("opsadm", "opsadm@dimagi.com", "pw")      # admin of ops only
    stranger = mk("stranger", "s@partner.org", "pw")
    other_org = Workspace.objects.create(slug="partner", display_name="Partner", created_by=stranger)
    M.objects.create(user=kid, workspace=w["connect"], role=M.VIEWER)
    M.objects.create(user=ops_admin, workspace=w["ops"], role=M.ADMIN)
    M.objects.create(user=stranger, workspace=other_org, role=M.OWNER)
    return {**w, "kid": kid, "ops_admin": ops_admin, "stranger": stranger, "partner": other_org}


def _below(scope, role=roles.READ_WRITE, **kw):
    return _line(scope, role, kind=G.WORKSPACE, subject_workspace=kw.pop("ws"), include_below=True, **kw)


def test_without_the_option_a_workspace_line_misses_people_only_in_a_child(tree):
    _line(tree["hal"], roles.READ_WRITE, kind=G.WORKSPACE, subject_workspace=tree["root"])
    assert grants.resolve(tree["hal"], _asker(tree["kid"])).role is None


def test_with_the_option_everyone_in_a_workspace_or_any_below_it_matches(tree):
    _below(tree["hal"], ws=tree["root"])
    assert grants.resolve(tree["hal"], _asker(tree["kid"])).role == roles.READ_WRITE       # only in connect
    assert grants.resolve(tree["hal"], _asker(tree["ops_admin"])).role == roles.READ_WRITE  # only in ops
    assert grants.resolve(tree["hal"], _asker(tree["vw"])).role == roles.READ_WRITE        # in dimagi itself


def test_the_subtree_never_crosses_organisations_or_goes_up(tree):
    _below(tree["hal"], ws=tree["root"])
    assert grants.resolve(tree["hal"], _asker(tree["stranger"])).role is None               # another org
    assert grants.resolve(tree["hal"], _asker(tree["out"])).role is None                    # nowhere
    _line(tree["hal"], roles.EDITOR, kind=G.WORKSPACE, subject_workspace=tree["connect"], include_below=True)
    # A line on connect (a leaf) reaches connect only: not dimagi above it, not ops beside it.
    assert grants.resolve(tree["hal"], _asker(tree["ops_admin"])).role == roles.READ_WRITE  # via the dimagi line
    G.objects.filter(agent=tree["hal"], subject_workspace=tree["root"]).delete()
    assert grants.resolve(tree["hal"], _asker(tree["ops_admin"])).role is None
    assert grants.resolve(tree["hal"], _asker(tree["vw"])).role is None                     # dimagi viewer, above connect
    assert grants.resolve(tree["hal"], _asker(tree["kid"])).role == roles.EDITOR


def test_the_floor_and_the_domain_still_apply_in_the_subtree(tree):
    _below(tree["hal"], ws=tree["root"], at_least=M.ADMIN)
    assert grants.resolve(tree["hal"], _asker(tree["kid"])).role is None                    # a viewer there
    assert grants.resolve(tree["hal"], _asker(tree["ops_admin"])).role == roles.READ_WRITE  # an admin of ops
    G.objects.filter(agent=tree["hal"]).delete()
    _below(tree["hal"], ws=tree["root"], domain="partner.org")
    assert grants.resolve(tree["hal"], _asker(tree["kid"])).role is None                    # wrong address
    assert grants.resolve(tree["hal"], _asker(tree["kid"], email="kid@partner.org")).role == roles.READ_WRITE


def test_a_subtree_line_is_a_different_line_from_the_plain_one(tree):
    _line(tree["hal"], roles.READ_ONLY, kind=G.WORKSPACE, subject_workspace=tree["root"])
    _below(tree["hal"], ws=tree["root"])            # same workspace and floor: its own line, not a clash
    assert G.objects.filter(agent=tree["hal"]).count() == 2
    assert grants.describe(G.objects.get(agent=tree["hal"], include_below=True)) \
        == "Everyone in dimagi or any workspace below it"


def test_bulk_read_answers_the_same_for_a_subtree_line(tree):
    from apps.workspaces import services as wsvc

    _below(tree["hal"], ws=tree["root"])
    lines = grants.lines_for(tree["hal"])
    below = {"dimagi": frozenset({"connect", "ops"})}
    for user in (tree["kid"], tree["ops_admin"], tree["stranger"], tree["vw"], tree["out"]):
        bulk = Asker(user=user, email=user.email, verified=True,
                     workspace_roles=wsvc.member_roles_of([user])[user.pk], descendants=below)
        assert grants.resolve(tree["hal"], bulk, lines) == grants.resolve(tree["hal"], _asker(user), lines)


def test_check_line_takes_the_option_only_on_a_workspace_line(tree):
    got = grants.check_line(tree["boss"], home="dimagi", kind=G.WORKSPACE, role=roles.READ_WRITE, workspace="dimagi",
                            include_below=True)
    assert got["include_below"] is True
    with pytest.raises(grants.LineError, match="only applies to a line naming a workspace"):
        grants.check_line(tree["boss"], home="dimagi", kind=G.DOMAIN, role=roles.READ_ONLY, domain="dimagi.com",
                          include_below=True)
