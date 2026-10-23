"""Alert source `canopy_projects_off`: your runner holds repos, but you have not
chosen any to use canopy in.

Canopy is opt-in per repo (`ProjectOptIn`, docs/architecture/runners.md
§ Opted-in projects), and no choice is nothing — so a person who pairs a box and
never opens Settings sees a runner that takes no repo work and mirrors no
session, with nothing saying why. This says why, to the one person who can fix
it: the runner's owner (the setting is theirs alone). `setup` severity: nothing
is broken, canopy is just not on yet.

One alert per owner, not per runner. Resolves when they opt into anything
(every repo, or at least one), or when they own no live runner that reports a
repo. Agents' turns are not affected (an agent's repo is always canopy's), so a
box that only runs agents is never the reason for this alert.
"""
from __future__ import annotations

from apps.attention.alerts import DISMISS, Action, Raise

from .models import ProjectOptIn, Runner

KIND = "canopy_projects_off"
SEVERITY = "setup"
CONFIRM_WITHIN = None
CLOSING_ACTION_IDS = ("choose",)

#: Settings → Canopy projects (`CanopyProjectsPanel`'s anchor), app-relative.
SETTINGS_PATH = "/settings#canopy-projects"


def _opted_in_ids() -> set:
    """Owners who chose something: every repo, or a non-empty list."""
    return set(ProjectOptIn.objects.filter(everything=True).values_list("user_id", flat=True)) | {
        uid for uid, projects in ProjectOptIn.objects.filter(everything=False).values_list("user_id", "projects")
        if projects
    }


def reconcile(now) -> list[Raise]:
    chose = _opted_in_ids()
    boxes: dict = {}
    for runner in (Runner.objects.exclude(status=Runner.RETIRED)
                   .filter(owner__isnull=False, workspace_id__isnull=False)
                   .exclude(owner_id__in=chose).order_by("name")):
        if runner.observed_projects():
            boxes.setdefault(runner.owner_id, []).append(runner)
    out: list[Raise] = []
    for owner_id, runners in boxes.items():
        held = sorted({p for r in runners for p in r.observed_projects()})
        names = ", ".join(r.name for r in runners)
        out.append(Raise(
            subject_key=f"user:{owner_id}",
            workspace=runners[0].workspace_id,
            title=f"Canopy is off in every repo on {names}",
            url=SETTINGS_PATH,
            scopes=(*[f"runner:{r.pk}" for r in runners],
                    *sorted({f"workspace:{r.workspace_id}" for r in runners})),
            body=(f"Choose which repos to use canopy in. Until then these runners take no repo work "
                  f"and mirror no sessions. They hold {len(held)} repo{'s' if len(held) != 1 else ''}."),
            fingerprint="off",
        ))
    return out


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False) or alert.subject_key != f"user:{user.pk}":
        return []
    return [Action("choose", "Choose repos", True, href=SETTINGS_PATH), DISMISS]


def perform(alert, user, action_id) -> None:
    raise ValueError(f"canopy_projects_off has no handler for {action_id!r}")
