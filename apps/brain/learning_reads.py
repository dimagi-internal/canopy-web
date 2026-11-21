""""Learning reads": whether learners may read a workspace's sessions at all, per workspace (`WorkspaceLearningReads`).

Off unless set. A workspace with no setting of its own inherits the nearest ancestor's that has one; a root with
none is off. `eligibility.explain` and `eligible_sources` ask `is_on` / `on_slugs` of a source's own workspace, so
every learning read (a run's `brain_read`, the project readers' leg, the graph's would-be links, the automatic
association layers) follows the one answer. Set on `/w/<ws>/brain` by the workspace's owners and admins, or an
ancestor's (`perms.BRAIN_MANAGE`), as a person only (`api_admin.set_learning_reads`), each change a `SettingChange`.

Nothing is cached: a change is the next request's answer. The tree is a handful of rows, read whole."""
from __future__ import annotations

from dataclasses import dataclass

from apps.workspaces import services as wsvc
from apps.workspaces.models import MAX_DEPTH, Workspace

from .models import PROJECT, WorkspaceLearningReads


@dataclass(frozen=True)
class Effective:
    on: bool
    #: The workspace whose setting decides it ("" when none does: off by default).
    source: str
    #: This workspace's own setting, or None when it inherits.
    own: bool | None


def _resolve(slug: str, parents: dict, own: dict) -> Effective:
    cur, steps = slug, 0
    while cur and steps <= MAX_DEPTH:
        if cur in own:
            return Effective(own[cur], cur, own.get(slug))
        cur, steps = parents.get(cur), steps + 1
    return Effective(False, "", own.get(slug))


def _own() -> dict[str, bool]:
    return dict(WorkspaceLearningReads.objects.values_list("workspace_id", "on"))


def effective(slug: str) -> Effective:
    """`slug`'s answer, where it comes from and its own setting."""
    own = _own()
    parents = dict(Workspace.objects.values_list("slug", "parent_id")) if own else {}
    return _resolve(slug, parents, own)


def is_on(slug: str) -> bool:
    return effective(slug).on


def any_on() -> bool:
    """Is any workspace on? One indexed read: nothing is on anywhere unless some workspace sets it on."""
    return WorkspaceLearningReads.objects.filter(on=True).exists()


def on_slugs() -> set[str]:
    """Every workspace that is effectively on, for a listing's SQL (`eligible_sources`)."""
    own = _own()
    if not any(own.values()):
        return set()
    parents = dict(Workspace.objects.values_list("slug", "parent_id"))
    return {s for s in parents if _resolve(s, parents, own).on}


def area(cap, subject=None, scope: str = "") -> set[str] | None:
    """The workspaces a run of `cap` reads in: `scope`'s subtree, else a project's own subtree, else None (anywhere)."""
    from apps.agents.models import AgentProject  # noqa: PLC0415 — as the other lazy imports of this app

    if not scope and cap.purpose_class == PROJECT and subject is not None:
        project = subject if isinstance(subject, AgentProject) else AgentProject.objects.filter(pk=subject).first()
        scope = project.workspace_id if project is not None else ""
    return ({scope} | wsvc.descendant_slugs({scope})) if scope else None


def run_refusal(cap, subject=None, scope: str = "") -> str:
    """Why a run of `cap` (about `subject`, reading `scope`'s subtree) may read nothing at all: "Learning reads" is
    off in every workspace it reads in; "" when some is on (each source is still asked, `eligibility.explain`)."""
    where, on = area(cap, subject, scope), on_slugs()
    if where is None and not on:
        return "learning reads are off in every workspace"
    if where is not None and not (where & on):
        return (f"learning reads are off in {scope or 'its workspace'} and the workspaces below it; its owners or "
                "admins turn them on on its brain page")
    return ""


def set_reads(workspace: Workspace, value: bool | None, *, by) -> tuple[Effective, Effective]:
    """Set `workspace`'s own setting (None: inherit again). Returns (before, after)."""
    before = effective(workspace.slug)
    if value is None:
        WorkspaceLearningReads.objects.filter(workspace=workspace).delete()
    else:
        WorkspaceLearningReads.objects.update_or_create(workspace=workspace,
                                                        defaults={"on": bool(value), "updated_by": by})
    return before, effective(workspace.slug)
