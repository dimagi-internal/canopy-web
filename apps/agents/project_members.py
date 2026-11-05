"""Who a project's people are: its first-class human membership.

A project has member AGENTS (`member_agents`) and, here, member PEOPLE: its lead
(`owner_user`, as `owner`) and the people added by name (`ProjectMember`, `admin` or
`member`). `ProjectParticipant` is a different thing: who canopy has SEEN take part,
filled in automatically, granting nothing.

Being a member means two things: you may change the project, and you are a member of
every vault attached to it (`vault_members`, § the attached link). So who may change the
list is deliberately narrower than who may edit the project: giving someone a vault is
not an editor's call, and never an agent's.
"""
from __future__ import annotations

from django.db import transaction

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from .member_roles import ASSIGNABLE, Membership, MembershipError, add, is_admin, not_a_person, role_named
from .member_roles import OWNER as LEAD
from .models import AgentProject, ProjectMember


def _readers(project: AgentProject) -> set[str]:
    from .services import project_reader_slugs  # noqa: PLC0415 — services imports credentials

    return project_reader_slugs(project)


def members(project: AgentProject) -> dict[int, Membership]:
    """Every member of `project`, by user id. A person counts only while they can read
    the project: leaving its workspace, or a share ending, ends it (the row stays, as a
    vault's does, and counts again if they come back). People only."""
    # A personal project has only its lead (`services.may_read_project`).
    rows = [] if project.is_personal else list(ProjectMember.objects.filter(project=project).select_related("user"))
    lead = project.owner_user if project.owner_user_id else None
    roles = wsvc.member_roles_of([r.user for r in rows] + ([lead] if lead else []))
    readers = _readers(project)
    out: dict[int, Membership] = {}
    if lead is not None and readers & set(roles.get(lead.pk, {})):
        add(out, lead, LEAD, "lead")
    for row in rows:
        if readers & set(roles.get(row.user_id, {})):
            add(out, row.user, row.role, "direct")
    for uid in not_a_person(out):
        out.pop(uid, None)
    return out


def is_member(project: AgentProject, user) -> bool:
    return getattr(user, "is_authenticated", False) and user.pk in members(project)


def may_manage(project: AgentProject, user) -> bool:
    """May change who is on the project: its lead, its admins, or an owner of its workspace.
    Never an agent's login, and not merely an editor: this list opens the project's vaults."""
    if not getattr(user, "is_authenticated", False) or not_a_person([user.pk]):
        return False
    m = members(project).get(user.pk)
    return (m is not None and is_admin(m.role)) or perms.can(user, project.workspace_id, perms.OWN)


def add_member(project: AgentProject, user, *, role: str, by) -> ProjectMember:
    """Add `user` by name (or change their role). They must be able to read the project."""
    if project.is_personal:
        raise MembershipError(422, "project_is_personal", "a personal project has only its lead: "
                                                          "make it a workspace project first")
    role = role_named(role)
    if role not in ASSIGNABLE:
        raise MembershipError(422, "project_bad_role", "role must be admin or member (the lead is set on the project)")
    if not_a_person([user.pk]):
        raise MembershipError(422, "project_not_a_person", f"{user.email} is an agent's login, not a person: "
                                                           "an agent joins a project as a member agent")
    if not _readers(project) & set(wsvc.member_roles_of([user]).get(user.pk, {})):
        raise MembershipError(422, "project_not_in_reach", f"{user.email} is not in a workspace that can read "
                                                           f"{project.address}")
    with transaction.atomic():
        row, _ = ProjectMember.objects.update_or_create(project=project, user=user,
                                                        defaults={"role": role, "added_by": by})
    return row


def remove_member(project: AgentProject, user_id: int) -> bool:
    """Remove a by-name membership. The lead stays the lead: that is changed on the project."""
    deleted, _ = ProjectMember.objects.filter(project=project, user_id=user_id).delete()
    return bool(deleted)
