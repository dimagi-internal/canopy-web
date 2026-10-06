"""Business logic for shareouts — kept out of the Ninja router so it's
unit-testable without HTTP."""
from __future__ import annotations

import datetime as dt

from django.db.models import Q
from django.utils import timezone

from .models import Shareout


def _aware(value):
    """Treat a naive datetime as UTC so a client that posts `...T09:15:00`
    (no offset) doesn't trip Django's naive-datetime warning or land in the
    server's local zone."""
    if isinstance(value, dt.datetime) and timezone.is_naive(value):
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def upsert_shareouts(items: list, *, workspace=None, created_by=None) -> dict:
    """Create shareouts, replacing prior rows in the same group.

    Idempotency group = (workspace, created_by, project, period_start,
    period_end, source). For each distinct group present in the incoming batch
    we delete pre-existing rows in that group once (counted as `replaced`)
    before creating the new ones, so re-running a period from the same source
    overwrites rather than duplicates. Scoping the group to `workspace` keeps a
    re-post in one tenant from touching another tenant's rows; scoping it to
    `created_by` keeps one person's re-post from deleting a teammate's briefing
    that happens to share a period and a source tag (`canopy:shareout` is the
    same source for everybody).

    `items` is a list of ShareoutIn-like objects (anything with the attribute
    names). `project_slug` is stored as given (the schema checks its shape);
    there is no project registry to resolve it against. `workspace` is the
    tenant these rows belong to (assigned on create).

    Returns {created, replaced}.
    """
    created = replaced = 0
    cleared_groups: set[tuple] = set()

    for item in items:
        project_slug = item.project_slug or None
        period_start = _aware(item.period_start)
        period_end = _aware(item.period_end)
        group = (
            workspace.pk if workspace else None,
            created_by.pk if created_by else None,
            project_slug,
            period_start,
            period_end,
            item.source,
        )
        if group not in cleared_groups:
            existing = Shareout.objects.filter(
                workspace=workspace,
                created_by=created_by,
                project_slug=project_slug,
                period_start=period_start,
                period_end=period_end,
                source=item.source,
            )
            replaced += existing.count()
            existing.delete()
            cleared_groups.add(group)

        Shareout.objects.create(
            workspace=workspace,
            project_slug=project_slug,
            period_start=period_start,
            period_end=period_end,
            title=item.title,
            summary=item.summary,
            content=item.content,
            links=[link.model_dump() for link in item.links],
            all_prs=[pr.model_dump() for pr in item.all_prs],
            author=item.author,
            produced_by_agent=getattr(item, "produced_by_agent", "") or "",
            source=item.source,
            created_by=created_by,
        )
        created += 1

    return {"created": created, "replaced": replaced}


def clear_shareouts(
    *,
    workspace_slugs: set[str],
    own_only_slugs: set[str] = frozenset(),
    user=None,
    source: str | None = None,
    project: str | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
) -> int:
    """Delete shareouts matching the filters (AND-combined); return the count.

    - workspace_slugs: REQUIRED tenant boundary — workspaces in which EVERY
                       matching row may be deleted (the caller owns them).
    - own_only_slugs:  workspaces in which only rows `user` posted may be
                       deleted (the caller is an editor there). Rows outside
                       both sets are never touched, so a no-filter clear wipes
                       what the caller may wipe and nothing else.
    - source:          exact source match (e.g. a prior run's source tag)
    - project:         project slug exact match
    - date_from:       period_end date >= date_from
    - date_to:         period_start date <= date_to
    """
    scope = Q(workspace_id__in=workspace_slugs)
    if own_only_slugs and user is not None:
        scope |= Q(workspace_id__in=own_only_slugs, created_by=user)
    qs = Shareout.objects.filter(scope)
    if source:
        qs = qs.filter(source=source)
    if project:
        qs = qs.filter(project_slug=project)
    if date_from is not None:
        qs = qs.filter(period_end__date__gte=date_from)
    if date_to is not None:
        qs = qs.filter(period_start__date__lte=date_to)
    count = qs.count()
    qs.delete()
    return count


def list_shareouts(
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    project: str | None = None,
    limit: int = 100,
    workspace_slugs: set[str] | None = None,
) -> list[dict]:
    """Return up to `limit` shareouts (newest period first) as dicts shaped
    for ShareoutOut. Filters AND-combine.

    - date_from:       period_end >= date_from
    - date_to:         period_start <= date_to
    - project:         project slug exact match (does not include roll-ups)
    - workspace_slugs: when provided, only rows in these tenants (a caller's
                       memberships, or the single pinned /w/{ws}); None = no
                       tenant scoping (used by non-scoped callers/tests).
    """
    limit = min(max(limit, 0), 500)
    qs = Shareout.objects.all()
    if workspace_slugs is not None:
        qs = qs.filter(workspace_id__in=workspace_slugs)
    if date_from is not None:
        qs = qs.filter(period_end__date__gte=date_from)
    if date_to is not None:
        qs = qs.filter(period_start__date__lte=date_to)
    if project:
        qs = qs.filter(project_slug=project)

    return [
        {
            "id": s.pk,
            "project_slug": s.project_slug or None,
            "period_start": s.period_start,
            "period_end": s.period_end,
            "title": s.title,
            "summary": s.summary,
            "content": s.content,
            "links": s.links or [],
            "all_prs": s.all_prs or [],
            "author": s.author,
            "produced_by_agent": s.produced_by_agent,
            "source": s.source,
            "created_at": s.created_at,
        }
        for s in qs[:limit]
    ]
