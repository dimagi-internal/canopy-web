"""Saving, versioning and restoring dynamic views. Routes are in `api.py`."""
from __future__ import annotations

import re

from django.db import transaction

from . import catalog
from .models import DynamicView, DynamicViewRevision

#: Render code larger than this is refused: a view is a screen, not an app bundle.
MAX_CODE_BYTES = 200_000
_DEFINES_VIEW = re.compile(r"\bfunction\s+View\s*\(|\b(?:const|let|var)\s+View\s*=")

EDITABLE = ("title", "description", "code", "sources", "slug", "project")


def validate_code(code: str) -> str:
    code = code or ""
    if not code.strip():
        raise ValueError("code is required: JSX defining `function View(props)`")
    if len(code.encode()) > MAX_CODE_BYTES:
        raise ValueError(f"code is larger than {MAX_CODE_BYTES} bytes")
    if not _DEFINES_VIEW.search(code):
        raise ValueError("code must define `function View(props)` (or `const View = ...`)")
    return code


def validate_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not re.fullmatch(DynamicView.SLUG_RE, slug):
        raise ValueError("slug must be 2-60 characters: lowercase letters, digits and hyphens")
    return slug


def slugify(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60].strip("-")
    return base if len(base) >= 2 else "view"


def unique_slug(workspace_id: str, wanted: str, *, exclude_pk: int | None = None) -> str:
    taken = set(DynamicView.objects.filter(workspace_id=workspace_id).exclude(pk=exclude_pk)
                .values_list("slug", flat=True))
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted[:56]}-{n}" in taken:
        n += 1
    return f"{wanted[:56]}-{n}"


def _snapshot(view: DynamicView, user, note: str) -> DynamicViewRevision:
    return DynamicViewRevision.objects.create(
        view=view, version=view.version, title=view.title, description=view.description,
        code=view.code, sources=view.sources, note=note[:300],
        changed_by=user if getattr(user, "is_authenticated", False) else None,
    )


@transaction.atomic
def create(*, workspace_id: str, project, data: dict, user) -> DynamicView:
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    explicit = (data.get("slug") or "").strip()
    slug = validate_slug(explicit) if explicit else unique_slug(workspace_id, slugify(title))
    if explicit and DynamicView.objects.filter(workspace_id=workspace_id, slug=slug).exists():
        raise ValueError(f"a view with slug '{slug}' already exists in this workspace")
    who = user if getattr(user, "is_authenticated", False) else None
    view = DynamicView.objects.create(
        workspace_id=workspace_id, project=project, slug=slug, title=title[:200],
        description=data.get("description") or "", code=validate_code(data.get("code") or ""),
        sources=catalog.validate_sources(data.get("sources")), version=1,
        created_by=who, updated_by=who,
    )
    _snapshot(view, user, data.get("note") or "created")
    return view


@transaction.atomic
def update(view: DynamicView, *, data: dict, user, project=None, project_given: bool = False) -> DynamicView:
    """Apply a partial change and record it as the next version. A change that
    alters nothing is not a version."""
    view = DynamicView.objects.select_for_update().get(pk=view.pk)
    changed = False
    if "title" in data and data["title"] is not None:
        title = data["title"].strip()
        if not title:
            raise ValueError("title cannot be blank")
        changed |= title != view.title
        view.title = title[:200]
    if "description" in data and data["description"] is not None:
        changed |= data["description"] != view.description
        view.description = data["description"]
    if "code" in data and data["code"] is not None:
        code = validate_code(data["code"])
        changed |= code != view.code
        view.code = code
    if "sources" in data and data["sources"] is not None:
        sources = catalog.validate_sources(data["sources"])
        changed |= sources != view.sources
        view.sources = sources
    if "slug" in data and data["slug"]:
        slug = validate_slug(data["slug"])
        if slug != view.slug and DynamicView.objects.filter(workspace_id=view.workspace_id, slug=slug).exists():
            raise ValueError(f"a view with slug '{slug}' already exists in this workspace")
        changed |= slug != view.slug
        view.slug = slug
    if project_given:
        changed |= (project.pk if project else None) != view.project_id
        view.project = project
    if not changed:
        return view
    view.version += 1
    view.updated_by = user if getattr(user, "is_authenticated", False) else None
    view.save()
    _snapshot(view, user, data.get("note") or "")
    return view


@transaction.atomic
def rollback(view: DynamicView, *, version: int, user) -> DynamicView:
    """Restore an earlier version's title, description, code and sources as a NEW
    version — history only grows, so a rollback can itself be undone."""
    rev = view.revisions.filter(version=version).first()
    if rev is None:
        raise LookupError(f"version {version} not found")
    return update(view, user=user, data={
        "title": rev.title, "description": rev.description, "code": rev.code,
        "sources": rev.sources, "note": f"restored version {version}",
    })
