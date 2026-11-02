"""One role -> permission-set engine under both agent roles and workspace roles.

`apps/common/rolesets.py` is the mechanism (a vocabulary, a table of roles, an implication rule,
`allows`, `holds`, rank); agents and workspaces each declare their own vocabulary on it. Pure
Python: no database.
"""
from __future__ import annotations

import pytest

from apps.agents import roles as agent_roles
from apps.common.rolesets import RoleSet, UnknownPermission
from apps.workspaces import permissions as wperms
from apps.workspaces.models import WorkspaceMembership as M


def _toy() -> RoleSet:
    return RoleSet("toy", {"read", "write", "admin"},
                   {"reader": {"read"}, "writer": {"write"}, "boss": {"admin"}},
                   implies={"write": {"read"}, "admin": {"write"}}, scopes=("use",))


def test_roles_are_closed_under_the_implication_rule():
    toy = _toy()
    assert toy.permissions_of("writer") == {"write", "read"}
    assert toy.permissions_of("boss") == {"admin", "write", "read"}      # transitively
    assert toy.close({"admin"}) == {"admin", "write", "read"}
    assert toy.rank("reader") < toy.rank("writer") < toy.rank("boss")    # fewest permissions first


def test_allows_holds_and_unknowns():
    toy = _toy()
    assert toy.allows("writer", "read") and not toy.allows("reader", "write")
    assert not toy.allows(None, "read") and not toy.allows("nobody", "read")
    assert toy.holds({"write"}, "write") and not toy.holds(set(), "write")
    with pytest.raises(UnknownPermission):
        toy.allows("writer", "wirte")             # a typo is loud, never "no"
    for family in (KeyError, ValueError):         # both families, so each module's old contract holds
        with pytest.raises(family):
            toy.holds({"read"}, "nope")


def test_a_scoped_permission_and_its_wildcard():
    toy = RoleSet("toy", {"write"}, {"w": {"write"}}, implies={"write": {"use:*"}}, scopes=("use",))
    assert toy.holds(toy.permissions_of("w"), "use:anything")
    assert toy.holds({"use:a"}, "use:a") and not toy.holds({"use:a"}, "use:b")
    with pytest.raises(UnknownPermission):
        toy.holds(set(), "other:a")               # a scope nobody declared
    with pytest.raises(UnknownPermission):
        toy.holds(set(), "use:*")                 # you ask about a name, not the wildcard


def test_a_ladder_adds_permissions_step_by_step():
    lad = RoleSet.ladder("lad", {"a", "b", "c"}, [("low", {"a"}), ("mid", {"b"}), ("high", {"c"})])
    assert lad.permissions_of("mid") == {"a", "b"} and lad.permissions_of("high") == {"a", "b", "c"}
    assert lad.at_least("high", "mid") and not lad.at_least("low", "mid") and not lad.at_least(None, "low")
    assert lad.lowest_with("b") == "mid"
    assert lad.strongest(["low", None, "high", "mid"]) == "high"


def test_the_order_must_name_exactly_the_roles():
    with pytest.raises(ValueError):
        RoleSet("bad", {"a"}, {"x": {"a"}}, order=["x", "y"])


# --- both sides are built on it ----------------------------------------------------------

def test_the_agent_and_the_workspace_both_use_the_one_engine():
    assert isinstance(agent_roles.ROLES, RoleSet) and isinstance(wperms.ROLES, RoleSet)


def test_the_workspace_table_is_derived_from_minimum_role_and_keeps_its_ladder():
    """Share the mechanism, not the vocabulary: viewer < editor < admin < owner (< superuser) and
    every capability's tier are exactly as `MINIMUM_ROLE` says."""
    assert wperms.ROLES.roles == (M.VIEWER, M.EDITOR, M.ADMIN, M.OWNER, M.SUPERUSER)
    for role in M.ROLE_RANK:
        for capability, minimum in wperms.MINIMUM_ROLE.items():
            assert wperms.role_allows(role, capability) == (M.ROLE_RANK[role] >= M.ROLE_RANK[minimum]), \
                (role, capability)
    assert not wperms.role_allows(None, wperms.READ)
    with pytest.raises(KeyError):
        wperms.role_allows(M.OWNER, "no.such.capability")


def test_workspace_role_floors_and_member_management_are_unchanged():
    assert wperms.role_meets(M.EDITOR, "") and not wperms.role_meets(None, "")
    assert wperms.role_meets(M.ADMIN, M.EDITOR) and not wperms.role_meets(M.VIEWER, M.EDITOR)
    assert wperms.is_role(M.OWNER) and not wperms.is_role(M.SUPERUSER) and not wperms.is_role("god")
    assert wperms.may_manage_member(M.ADMIN, M.EDITOR, M.VIEWER)
    assert not wperms.may_manage_member(M.ADMIN, M.ADMIN)
    assert not wperms.may_manage_member(M.ADMIN, M.VIEWER, M.ADMIN)
    assert wperms.may_manage_member(M.OWNER, M.ADMIN, M.OWNER)
    assert not wperms.may_manage_member(M.EDITOR, M.VIEWER)
