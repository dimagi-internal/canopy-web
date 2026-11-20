"""The browser extension's question: which of my apps have a page for this tab?

`GET /api/browser/apps?url=<tab url>` → every installed, enabled app the caller can
use, across their workspaces, with a `browser` entry matching the URL
(`browser.entries_for`). An entry is listed only when the caller holds the page's
role (`access.roles_for`), the same test that decides whether `get_workspace_app`
shows them that page, so the panel lists nothing it could not draw. A guest is asked
only about the apps their scope names. Design: docs/architecture/browser-extension.md.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import access, browser
from .models import AppInstallation
from .schemas import BrowserAppOut

router = Router(auth=session_auth, tags=["workspace-apps"])


def _rows(user, installations, url: str) -> list[dict]:
    rows = []
    for inst in installations:
        entries = browser.entries_for(inst.manifest, url)
        if not entries:
            continue
        # Read only for an app that has a page for this tab: a handful, never every app.
        roles = access.roles_for(user, inst)
        rows.extend({"inst": inst, **e} for e in entries if e["role"] in roles)
    return rows


@router.get("/apps", response=list[BrowserAppOut], summary="Your apps with a page for a browser tab")
@gate("member", "app-guest")
def list_browser_apps(request: HttpRequest, url: str) -> list[BrowserAppOut]:
    """The installed, enabled apps you can use, in every workspace you are in, whose pages
    declare (manifest `browser`) that they run beside a tab at `url`. An unusable or
    non-https URL matches nothing."""
    user = request.user
    if len(url) > browser.MAX_URL:
        return []
    with_browser = (AppInstallation.objects.filter(enabled=True, manifest__has_key="browser")
                    .select_related("workspace"))

    def read():
        return _rows(user, with_browser.filter(workspace_id__in=wsvc.request_workspace_slugs(request)), url)

    rows = wsvc.each_organisation(request, read)
    # A guest's apps: only the workspaces they are a guest of, only the apps their scope names.
    for slug, scope in wsvc.guest_scopes(user).items():
        keys = list((scope.get("apps") or {}))
        if keys:
            rows.extend(_rows(user, with_browser.filter(workspace_id=slug, key__in=keys), url))
    seen, out = set(), []
    for r in rows:
        inst = r["inst"]
        ident = (inst.workspace_id, inst.key, r["page"])
        if ident in seen:  # an organisation's run can overlap another's subtree
            continue
        seen.add(ident)
        ws: Workspace = inst.workspace
        out.append(BrowserAppOut(workspace=inst.workspace_id, workspace_name=ws.display_name or inst.workspace_id,
                                 app_key=inst.key, app_name=inst.name, page_slug=r["page"],
                                 page_title=r["page_title"], adapter=r["adapter"],
                                 capabilities=r["capabilities"], events=r["events"], matches=r["matches"]))
    out.sort(key=lambda b: (b.workspace_name.lower(), b.app_name.lower(), b.page_slug))
    return out
