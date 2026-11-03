"""A workspace's admission rules, under `/api/workspaces/{slug}/admission-…`.
The rules, their evaluation and their notices are in `rules`, `evaluate` and `notices`;
the gate is the workspace's."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.workspaces import permissions as perms
from apps.workspaces.gates import require

from . import rules
from .schemas import (
    AdmissionRuleIn,
    AdmissionRuleOut,
    AdmissionRulePatch,
    AdmissionRulesOut,
    AdmissionSettingsIn,
    AdmissionSettingsOut,
)

router = Router(auth=session_auth, tags=["workspaces"])


@router.get("/{slug}/admission-rules", response=AdmissionRulesOut,
            summary="Which turns this workspace refuses before they run")
@gate("member")  # read-only: anyone may know which turns are refused
def list_admission_rules(request: HttpRequest, slug: str) -> AdmissionRulesOut:
    """This workspace's admission rules: each one's conditions, action, notices,
    whether it is on, and how often it has fired. Any member may read them;
    changing them needs the admin role."""
    m = require(request.user, slug, perms.READ)
    return AdmissionRulesOut(
        **rules.policy_for(m.workspace, can_manage=perms.role_allows(m.role, perms.ADMISSION_MANAGE)),
    )


@router.put("/{slug}/admission-settings", response=AdmissionSettingsOut,
            summary="Switch automatic replies to rejected senders on or off")
@gate("admission.manage", setting=True)
def set_admission_settings(request: HttpRequest, slug: str,
                           payload: AdmissionSettingsIn) -> AdmissionSettingsOut:
    """On, a rule with "automatic reply to the sender" replies on the sender's
    email thread, from the agent's mailbox, in fixed text. Off, the reply is
    recorded and not sent. Needs the admin role."""
    m = require(request.user, slug, perms.ADMISSION_MANAGE)
    return AdmissionSettingsOut(sender_notices_enabled=rules.set_sender_notices(
        m.workspace, payload.sender_notices_enabled, user=request.user))


@router.post("/{slug}/admission-rules", response={201: AdmissionRuleOut},
             summary="Add an admission rule")
@gate("admission.manage", setting=True)
def create_admission_rule(request: HttpRequest, slug: str, payload: AdmissionRuleIn):
    """Blank conditions match anything, and at least one must be set. An
    `allow` rule exempts the turns it matches from every `reject` rule. 422 for
    an agent outside this workspace."""
    m = require(request.user, slug, perms.ADMISSION_MANAGE)
    try:
        rule = rules.create_rule(m.workspace, payload.model_dump(), user=request.user)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, AdmissionRuleOut(**rules.rule_out(rule)))


@router.patch("/{slug}/admission-rules/{rule_id}", response=AdmissionRuleOut,
              summary="Change an admission rule, or switch it on or off")
@gate("admission.manage", setting=True)
def update_admission_rule(request: HttpRequest, slug: str, rule_id: int,
                          payload: AdmissionRulePatch) -> AdmissionRuleOut:
    """Only the fields sent change."""
    m = require(request.user, slug, perms.ADMISSION_MANAGE)
    try:
        rule = rules.update_rule(m.workspace, rule_id, payload.model_dump(exclude_none=True),
                                 user=request.user)
    except LookupError as exc:
        raise HttpError(404, "no such rule in this workspace") from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return AdmissionRuleOut(**rules.rule_out(rule))


@router.delete("/{slug}/admission-rules/{rule_id}", response={204: None},
               summary="Remove an admission rule")
@gate("admission.manage", setting=True)
def delete_admission_rule(request: HttpRequest, slug: str, rule_id: int):
    """A removed default is not re-seeded. Disabling it keeps it one toggle away."""
    m = require(request.user, slug, perms.ADMISSION_MANAGE)
    try:
        rules.delete_rule(m.workspace, rule_id, user=request.user)
    except LookupError as exc:
        raise HttpError(404, "no such rule in this workspace") from exc
    return Status(204, None)

