"""Which of a runner's repos one person sees and may start work in.

Read by the fleet list, once for all its rows. The rules themselves live on
`Runner` (`project_workspaces`, `offers_project_in`) and in routing; this only
reads them for many runners at once and says what they mean for this caller.
docs/architecture/runners.md § Opted-in projects.
"""
from __future__ import annotations

from apps.agents.models import Agent
from apps.workspaces import services as wsvc

from .models import ProjectOptIn
from .routing import load_workspace_orders, runner_tenant_slugs_of


def annotate_for_caller(user, runners, administered) -> None:
    """Stamp each row for `user`: `repo_workspaces` ({repo: the workspaces they can
    start a repo chat in}), and on a box they do not administer narrow
    `capabilities["projects"]` to the repos it offers in their workspaces, plus
    their workspaces' agents' repos. The row is never saved."""
    runners = list(runners)
    if not runners:
        return
    mine = wsvc.user_workspace_slugs(user)
    opt_ins = {o.user_id: o for o in ProjectOptIn.objects.filter(user_id__in={r.owner_id for r in runners})}
    assigned: dict = {}
    for r in runners:
        setattr(r, r.OPT_IN_CACHE, opt_ins.get(r.owner_id) or ProjectOptIn(user_id=r.owner_id))
        assigned[r.pk] = {repo: r.project_workspaces(repo) for repo in r.project_names()}
    below = wsvc.descendant_map({s for repos in assigned.values() for slugs in repos.values() for s in slugs})
    orders = load_workspace_orders(mine)
    listed = {ws: {row.pk for row in rows} for ws, rows in orders.items()}
    tenants = runner_tenant_slugs_of(runners)
    agent_repos = set(Agent.objects.filter(workspace_id__in=mine).values_list("slug", flat=True))
    for r in runners:
        starts: dict[str, list[str]] = {}
        offered: set[str] = set()
        for repo, slugs in assigned[r.pk].items():
            reach = (set(slugs).union(*(below[s] for s in slugs)) if slugs else set()) & mine
            if reach:
                offered.add(repo)
            # Where they meet the assignment first: its own workspace when they
            # are in it, and only otherwise the ones below it.
            usable = [ws for ws in (set(slugs) & mine or reach) & tenants[r.pk]
                      if r.pk in administered or r.pk in listed.get(ws, ())]
            if usable:
                starts[repo] = sorted(usable)
        r.repo_workspaces = starts
        if r.pk not in administered:
            shown = offered | (set(r.observed_projects()) & agent_repos)
            r.capabilities = {**(r.capabilities or {}),
                              "projects": [p for p in r.observed_projects() if p in shown]}
