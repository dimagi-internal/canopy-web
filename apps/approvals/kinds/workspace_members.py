"""Kinds: add someone to a workspace, change a member's role.

The authority is the approver's own, checked the way the members routes check it: they must hold
`members.manage` in the workspace and may only grant or change roles strictly below their own
(`permissions.may_manage_member`). The person the request is about can never approve it.
"""
from __future__ import annotations

from typing import Literal

from apps.common.schemas import StrictModel
from apps.workspaces import invites, notifications
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import Described, ExecError, Kind, KindError, register, user_by_email

#: Roles a request may name. (`owner` is changed only by an owner, which `may_manage_member` enforces.)
Role = Literal["viewer", "editor", "admin", "owner"]


class AddParams(StrictModel):
    workspace: str
    email: str
    role: Role


class SetRoleParams(StrictModel):
    workspace: str
    email: str
    role: Role


def _workspace(slug: str) -> Workspace:
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None:
        raise KindError(f"there is no workspace {slug!r}")
    return ws


def _email(value: str) -> str:
    email = (value or "").strip().lower()
    if "@" not in email or " " in email:
        raise KindError(f"{value!r} is not an email address")
    return email


def _may_manage(user, slug: str, target_role: str | None, new_role: str) -> bool:
    mine = wsvc.member_role(user, slug)
    return bool(mine and perms.role_allows(mine, perms.MEMBERS_MANAGE)
                and perms.may_manage_member(mine, target_role, new_role))


@register
class WorkspaceMemberAdd(Kind):
    slug = "workspace.member.add"
    label = "Add someone to a workspace"
    params_model = AddParams
    needs = "an admin or owner of the workspace (only an owner can add an admin or owner)"
    does = ("Adds the person to the workspace at the role: directly if they already have a canopy "
            "account in this organisation, otherwise by emailing them an invitation.")

    def describe(self, p: AddParams) -> Described:
        ws = _workspace(p.workspace)
        email = _email(p.email)
        return Described(
            workspace=ws.slug,
            title=f"Add {email} to {ws.display_name} as {p.role}",
            summary=f"{email} would become {p.role} of {ws.display_name} ({ws.slug}).",
            plan=[f"{email} becomes {p.role} of {ws.display_name}",
                  "If they already have a canopy account in this organisation they are added now; "
                  "otherwise they are emailed an invitation and join when they accept it"],
            scopes=[f"workspace:{ws.slug}"], about_email=email)

    def may_approve(self, user, p: AddParams, d: Described) -> bool:
        return (user.email or "").lower() != d.about_email and _may_manage(user, p.workspace, None, p.role)

    def execute(self, user, p: AddParams, request) -> dict:
        ws = _workspace(p.workspace)
        email = _email(p.email)
        target = user_by_email(email)
        if target is not None:
            try:
                wsvc.add_tree_member(workspace=ws, user_id=target.pk, role=p.role,
                                     visible=set(wsvc.member_roles(user)), by=user)
                return {"added": email, "role": p.role}
            except wsvc.MemberError as exc:
                if exc.code == "already_member":
                    return {"already_member": email, "role": wsvc.member_role(target, ws.slug)}
                if exc.code != "not_found":
                    raise ExecError(f"could not add {email}: {exc.code}") from exc
                # Has an account, but not in this organisation: invite them.
        invite = invites.create_invite(workspace=ws, email=email, role=p.role, invited_by=user)
        return {"invited": email, "role": p.role, "email_status": notifications.email_invite(invite=invite)}


@register
class WorkspaceMemberSetRole(Kind):
    slug = "workspace.member.set_role"
    label = "Change a member's role in a workspace"
    params_model = SetRoleParams
    needs = "an admin or owner of the workspace (only an owner can touch an admin or owner)"
    does = "Sets an existing member's role. Setting the role they already hold changes nothing."

    def describe(self, p: SetRoleParams) -> Described:
        ws = _workspace(p.workspace)
        email = _email(p.email)
        target = user_by_email(email)
        current = wsvc.member_role(target, ws.slug) if target is not None else None
        if current is None:
            raise KindError(f"{email} is not a member of {ws.slug}; ask to add them instead")
        return Described(
            workspace=ws.slug,
            title=f"Change {email} from {current} to {p.role} in {ws.display_name}",
            summary=f"{email} is {current} of {ws.display_name} ({ws.slug}) today.",
            plan=[f"{email}'s role in {ws.display_name} changes from {current} to {p.role}"],
            scopes=[f"workspace:{ws.slug}"], about_email=email)

    def may_approve(self, user, p: SetRoleParams, d: Described) -> bool:
        target = user_by_email(d.about_email)
        current = wsvc.member_role(target, p.workspace) if target is not None else None
        return (user.email or "").lower() != d.about_email and current is not None and _may_manage(
            user, p.workspace, current, p.role)

    def execute(self, user, p: SetRoleParams, request) -> dict:
        ws = _workspace(p.workspace)
        target = user_by_email(_email(p.email))
        if target is None:
            raise ExecError(f"{p.email} is no longer on canopy")
        try:
            wsvc.set_member_role(workspace=ws, user_id=target.pk, role=p.role, by=user)
        except wsvc.MemberError as exc:
            raise ExecError({"last_owner": "that would leave the workspace with no owner",
                             "not_found": f"{p.email} is not a member of {ws.slug}",
                             "invalid_role": f"{p.role} is not a role"}.get(exc.code, exc.code)) from exc
        return {"email": p.email, "role": p.role}
