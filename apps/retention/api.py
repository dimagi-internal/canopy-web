"""A workspace's content retention rules, under `/api/workspaces/{slug}/retention…`.
The rules and the purge are in `rules` and `services`; the gate is the workspace's."""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.workspaces import permissions as perms
from apps.workspaces.gates import require

from . import rules
from . import services as retention_services
from .schemas import (
    RetentionOut,
    RetentionPreviewOut,
    RetentionRuleIn,
    RetentionRuleOut,
)

router = Router(auth=session_auth, tags=["workspaces"])


@router.get("/{slug}/retention", response=RetentionOut,
            summary="How long this workspace keeps chat and turn content")
@gate("member")  # read-only: everyone may know how long their chats are kept
def get_retention(request: HttpRequest, slug: str) -> RetentionOut:
    """This workspace's retention rules and the ones it inherits from the
    workspaces above it and the deployment. Any member may read them; changing
    them needs the admin role."""
    m = require(request.user, slug, perms.READ)
    return RetentionOut(**rules.policy_for(
        m.workspace, can_manage=perms.role_allows(m.role, perms.RETENTION_MANAGE)))


@router.get("/{slug}/retention/preview", response=RetentionPreviewOut,
            summary="What the current rules would delete from this workspace right now")
@gate("retention.manage")
def retention_preview(request: HttpRequest, slug: str) -> RetentionPreviewOut:
    """Counts only, per rule, of what would be dropped from this workspace's
    own chats and turns if the rules were enforced now. Deletes nothing."""
    m = require(request.user, slug, perms.RETENTION_MANAGE)
    tally = retention_services.preview(m.workspace.slug)
    return RetentionPreviewOut(
        workspace=m.workspace.slug,
        enforced=bool(getattr(settings, "CANOPY_RETENTION_ENFORCE", False)),
        totals=tally.totals(),
        by_rule=[
            {"rule_id": pk, "summary": rules.summary(tally.rules[pk]), "counts": dict(counts)}
            for pk, counts in sorted(tally.by_rule.items())
        ],
    )


@router.post("/{slug}/retention/rules", response={201: RetentionRuleOut},
             summary="Add a retention rule")
@gate("retention.manage", setting=True)
def create_retention_rule(request: HttpRequest, slug: str, payload: RetentionRuleIn):
    """Blank filters match anything; `keep_days` null keeps matching content
    forever. 422 if a rule with exactly these filters already exists, or the
    agent is not in this workspace."""
    m = require(request.user, slug, perms.RETENTION_MANAGE)
    try:
        rule = rules.create_rule(m.workspace, payload.model_dump(), user=request.user)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, RetentionRuleOut(**rules.rule_out(rule)))


@router.put("/{slug}/retention/rules/{rule_id}", response=RetentionRuleOut,
            summary="Change a retention rule")
@gate("retention.manage", setting=True)
def update_retention_rule(request: HttpRequest, slug: str, rule_id: int,
                          payload: RetentionRuleIn) -> RetentionRuleOut:
    """Replaces the rule's filters, retention and note."""
    m = require(request.user, slug, perms.RETENTION_MANAGE)
    try:
        rule = rules.update_rule(m.workspace, rule_id, payload.model_dump(), user=request.user)
    except LookupError as exc:
        raise HttpError(404, "no such rule in this workspace") from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return RetentionRuleOut(**rules.rule_out(rule))


@router.delete("/{slug}/retention/rules/{rule_id}", response={204: None},
               summary="Remove a retention rule")
@gate("retention.manage", setting=True)
def delete_retention_rule(request: HttpRequest, slug: str, rule_id: int):
    """Content the rule would have expired falls back to the next rule that
    matches, or is kept forever."""
    m = require(request.user, slug, perms.RETENTION_MANAGE)
    try:
        rules.delete_rule(m.workspace, rule_id, user=request.user)
    except LookupError as exc:
        raise HttpError(404, "no such rule in this workspace") from exc
    return Status(204, None)

