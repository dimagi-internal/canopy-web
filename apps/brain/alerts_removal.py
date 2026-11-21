"""Alert source `learning_removal`: something a learning run made that a removal could not undo.

When a session a run read is removed from learning, the cascade withdraws what canopy can undo of what that run made
and FLAGS the rest (`lineage.undo`): a sent email, a published update, a pull request, an issue, a link. One `action`
alert per flagged artifact, on its workspace's brain page, held by its owner (the person the run answered to), until
they or the workspace's managers acknowledge it: deciding what to do with it (close the PR, follow up on the mail) is
a person's. Ids and the link only: nothing a session said."""
from __future__ import annotations

from django.utils import timezone

from apps.attention.alerts import Action, Raise
from apps.workspaces import permissions as perms

from .models import BrainRunArtifact

KIND = "learning_removal"
SEVERITY = "action"
CONFIRM_WITHIN = None
CLOSING_ACTION_IDS = ("acknowledge",)


def brain_path(workspace: str) -> str:
    return f"/w/{workspace}/brain"


def _what(art: BrainRunArtifact) -> str:
    if art.kind == "github.pull":
        return f"a pull request ({art.ref})"
    if art.kind == "github.issue":
        return f"an issue ({art.ref})"
    if art.kind == "link":
        return f"a link it reported ({art.ref})"
    return f"a {art.kind.split('.', 1)[-1]} (#{art.ref})"


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    for art in (BrainRunArtifact.objects.filter(state=BrainRunArtifact.FLAGGED, acknowledged_at__isnull=True)
                .select_related("run__capability")):
        out.append(Raise(
            subject_key=f"brain-artifact:{art.pk}",
            workspace=art.workspace_id,
            title=f"A learning run's work could not be undone: {_what(art)}",
            body=(f"{art.run.capability.name} (run {art.run_id}) made it from sessions one of which has since been "
                  f"removed from learning ({art.reason or 'removed'}). canopy cannot take it back: decide whether it "
                  "should stand, then acknowledge."),
            url=brain_path(art.workspace_id),
            scopes=(f"workspace:{art.workspace_id}",),
            handler_user=art.owner_id,
            ask="Something a learning run made for you came from a session that was removed from learning.",
        ))
    return out


def _artifact_of(alert):
    parts = alert.subject_key.split(":")
    if len(parts) != 2 or parts[0] != "brain-artifact" or not parts[1].isdecimal():
        return None
    return BrainRunArtifact.objects.filter(pk=int(parts[1]), workspace_id=alert.workspace_id).first()


def may_acknowledge(user, art: BrainRunArtifact) -> bool:
    return getattr(user, "is_authenticated", False) and (
        art.owner_id == user.pk or bool(getattr(user, "is_superuser", False))
        or perms.can(user, art.workspace_id, perms.BRAIN_MANAGE))


def actions(alert, user) -> list[Action]:
    art = _artifact_of(alert)
    if art is None or not may_acknowledge(user, art):
        return []
    return [Action("acknowledge", "Acknowledge", True), Action("open", "Open the brain page", False,
                                                                 href=brain_path(art.workspace_id))]


def acknowledge(art: BrainRunArtifact, user) -> None:
    BrainRunArtifact.objects.filter(pk=art.pk, acknowledged_at__isnull=True).update(
        acknowledged_at=timezone.now(), acknowledged_by=user)


def perform(alert, user, action_id) -> None:
    art = _artifact_of(alert)
    if action_id != "acknowledge" or art is None:
        raise ValueError(f"learning_removal has no handler for {action_id!r}")
    acknowledge(art, user)
