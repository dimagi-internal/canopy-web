"""Workspace apps over REST (and so MCP): the store, installing, the draft → test →
publish life cycle, using an app, and the public door.

* Managing an app (install, on/off, public, remove) is `integrations` (admin).
* Maintaining it (drafts, changes, checks, publish, rollback) is `access.may_maintain`:
  managers, or a member granted the app's `maintainer` role (the head of HR).
* Using it is by the app's own roles (`access.roles_for`). `?draft=<id>` points a
  read or action at that draft's TEST COPY — sample records, nothing sent — and
  needs maintainer rights.
* The public door (`/api/public-apps/`) serves only an app that is enabled AND
  public: its public pages, public projections and create-only, rate-limited
  public actions. Anything else is the same 404, so it reveals nothing.

Every route is also an MCP tool: an agent changes an app for a person by reading
`…/guide` and POSTing changes to `…/draft/ops`. Design: docs/architecture/workspace-apps.md.
"""
from __future__ import annotations

import json
from typing import Any

from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from ninja import Body, File, Form, Router, Status
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import TYPE_RATE_LIMIT, ProblemError
from apps.api.route_gates import gate
from apps.embedding.audit import client_ip
from apps.workspaces import permissions as perms
from apps.workspaces.models import Workspace

from . import access, guide, services
from .models import LIVE, AppInstallation, AppRoleGrant, AppVersion
from .schemas import (
    ActionIn,
    BulkActionIn,
    DraftIn,
    DraftInfoOut,
    DraftOut,
    FileOut,
    GrantIn,
    GrantOut,
    GuideOut,
    HelperIn,
    HelperOut,
    InstallationOut,
    InstallationPatch,
    InstallationSummaryOut,
    InstallIn,
    LinkOut,
    LinkPageOut,
    PageOut,
    PublicAppOut,
    RollbackIn,
    SetupIn,
    SetupOut,
    StoreAppDetailOut,
    StoreAppOut,
    VersionOut,
)

router = Router(auth=session_auth, tags=["workspace-apps"])
public_router = Router(tags=["workspace-apps"])


# --- shared ---------------------------------------------------------------------------------


def _member_or_404(request: HttpRequest, workspace: str) -> None:
    if not perms.can(request.user, workspace, perms.READ):
        raise HttpError(404, f"workspace '{workspace}' not found")


def _installation(request: HttpRequest, workspace: str, key: str, *, enabled: bool = True) -> AppInstallation:
    """The app, for a member of its workspace. A switched-off app is 404 to its
    users; those who manage it still reach it (to switch it back on)."""
    _member_or_404(request, workspace)
    inst = (AppInstallation.objects.select_related("template", "live__template_version")
            .filter(workspace_id=workspace, key=key).first())
    if inst is None or (enabled and not inst.enabled and not access.may_manage(request.user, workspace)):
        raise HttpError(404, f"no app '{key}' in '{workspace}'")
    return inst


def _require_manage(request: HttpRequest, workspace: str) -> None:
    if not access.may_manage(request.user, workspace):
        raise HttpError(403, "managing apps needs the workspace's integrations permission (admin)")


def _require_maintain(request: HttpRequest, inst: AppInstallation) -> None:
    if not access.may_maintain(request.user, inst):
        raise HttpError(403, "changing this app needs its maintainer role, or the workspace's admin")


def _space(request: HttpRequest, inst: AppInstallation, draft: int | None) -> str:
    """`?draft=<id>` → that draft's test copy, for a maintainer; else live."""
    if not draft:
        return LIVE
    v = inst.versions.filter(pk=draft, status=AppVersion.DRAFT).first()
    if not (v is not None and access.is_helper(request.user, v)):
        _require_maintain(request, inst)
    if v is None:
        raise HttpError(404, "that draft is no longer open")
    return v.space


def _roles(request: HttpRequest, inst: AppInstallation, space: str, manifest: dict) -> set[str]:
    """The caller's app roles. In a draft's TEST COPY — reached only by a
    maintainer (`_space`), over sample records only — the maintainer holds every
    role of the app, so the person testing a change can try it as a recruiter,
    an interviewer and a member alike before anyone else sees it."""
    if space != LIVE:
        return {"public", "member", access.MAINTAINER, *(manifest.get("roles") or {})}
    return access.roles_for(request.user, inst, manifest)


def _refusal(exc: services.Refused) -> HttpError:
    return HttpError(exc.status, str(exc))


def _schema(m: dict) -> dict:
    """What a page needs to draw fields it was not written for."""
    return {name: {"label": c.get("label", name), "title_field": c["title_field"],
                   "fields": {k: {x: f[x] for x in ("type", "label", "help", "required", "hidden", "options",
                                                     "option_labels", "hidden_options", "collection", "accept",
                                                     "max_mb", "origin", "read") if x in f}
                              for k, f in c["fields"].items()},
                   "public_fields": (c.get("public") or {}).get("fields")}
            for name, c in m["collections"].items()} | {
        "_actions": {k: {"input": a["input"], "role": a["role"], "label": a["label"], "enabled": a["enabled"],
                         "collection": a["collection"], "op": a["op"], "params": a.get("params") or {},
                         "bulk": a.get("bulk", False), "when": a.get("when")} for k, a in m["actions"].items()},
        "_links": {k: {"collection": v["collection"], "label": v["label"], "days": v["days"]}
                   for k, v in (m.get("links") or {}).items()},
    }


def _settings(m: dict) -> dict:
    return {k: s.get("value", s.get("default")) for k, s in (m.get("settings") or {}).items()}


def _summary(inst: AppInstallation) -> dict:
    m = inst.manifest
    up = services.upgrade_available(inst)
    return dict(key=inst.key, name=inst.name, description=m.get("description", ""), workspace=inst.workspace_id,
                template=inst.template.key, template_version=inst.live.template_version.version,
                live_version=inst.live.number, enabled=inst.enabled, public=inst.public,
                has_public_pages=any(p["audience"] == "public" for p in m["pages"]),
                upgrade_available=up.version if up else None,
                has_draft=services.open_draft(inst) is not None,
                installed_at=inst.installed_at, updated_at=inst.updated_at)


def _out(request: HttpRequest, inst: AppInstallation, space: str = LIVE) -> InstallationOut:
    m = services.manifest_for(inst, space)
    roles = _roles(request, inst, space, m)
    maintain = access.may_maintain(request.user, inst)
    pages = [p for p in m["pages"] if (p["audience"] == "member" and p["role"] in roles) or p["audience"] == "public"]
    draft = None
    if space != LIVE:
        v = services.draft_for_space(inst, space)
        draft = DraftInfoOut(id=v.pk, number=v.number, origin=v.origin, template_version=v.template_version.version,
                             space=space)
    return InstallationOut(**_summary(inst), my_roles=sorted(roles), may_maintain=maintain,
                           may_manage=access.may_manage(request.user, inst.workspace_id),
                           pages=[PageOut(**p) for p in pages], data_model=_schema(m), settings=_settings(m),
                           draft=draft, manifest=m if maintain else None)


def _where(raw: str) -> dict:
    try:
        where = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise HttpError(422, "where must be a JSON object") from exc
    if not isinstance(where, dict):
        raise HttpError(422, "where must be a JSON object")
    return where


def _email(u) -> str | None:
    return u.email if u else None


# --- the store and installing -------------------------------------------------------------


@router.get("/workspace-app-store/{workspace}/", response=list[StoreAppOut],
            summary="The app store: apps this workspace can install")
@gate("member")
def list_app_store(request: HttpRequest, workspace: str) -> list[StoreAppOut]:
    """canopy's own apps, and this workspace's own templates."""
    _member_or_404(request, workspace)
    return [StoreAppOut(**a) for a in services.store(workspace)]


@router.get("/workspace-app-store/{workspace}/detail", response=StoreAppDetailOut,
            summary="One store app in full: screenshots, what it does, its roles, records and versions")
@gate("member")
def get_app_store_detail(request: HttpRequest, workspace: str, template: str) -> StoreAppDetailOut:
    """`template` is the store key (`hiring`, or `<workspace>/<key>` for the workspace's own)."""
    _member_or_404(request, workspace)
    try:
        return StoreAppDetailOut(**services.store_detail(workspace, template))
    except LookupError as exc:
        raise HttpError(404, str(exc)) from exc


@router.get("/workspace-app-store/{workspace}/screenshot", response={200: None},
            summary="A store app's screenshot, by the file its listing names")
@gate("member")
def get_app_store_screenshot(request: HttpRequest, workspace: str, template: str, file: str):
    _member_or_404(request, workspace)
    try:
        body, kind = services.store_screenshot(workspace, template, file)
    except LookupError as exc:
        raise HttpError(404, str(exc)) from exc
    response = HttpResponse(body, content_type=kind)
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/workspace-apps/{workspace}/", response=list[InstallationSummaryOut],
            summary="The apps installed in a workspace")
@gate("member")
def list_workspace_apps(request: HttpRequest, workspace: str) -> list[InstallationSummaryOut]:
    """Switched-off apps are listed only for those who manage apps here."""
    _member_or_404(request, workspace)
    services.sync_bundled()
    qs = AppInstallation.objects.filter(workspace_id=workspace).select_related("template", "live__template_version")
    if not access.may_manage(request.user, workspace):
        qs = qs.filter(enabled=True)
    return [InstallationSummaryOut(**_summary(i)) for i in qs]


@router.post("/workspace-apps/{workspace}/", response={201: InstallationOut}, summary="Install an app")
@gate("integrations", setting=True)
def install_workspace_app(request: HttpRequest, workspace: str, payload: InstallIn) -> Status:
    """`template` installs one from the store (`list_app_store`); `manifest`
    installs one you wrote. Either way it starts enabled and NOT public."""
    _member_or_404(request, workspace)
    _require_manage(request, workspace)
    try:
        inst = services.install(workspace_id=workspace, user=request.user, template=payload.template,
                                manifest=payload.manifest)
    except LookupError as exc:
        raise HttpError(404, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, _out(request, inst))


@router.get("/workspace-apps/{workspace}/{key}", response=InstallationOut,
            summary="One installed app: its pages for you, your roles, its fields and settings")
@gate("member")
def get_workspace_app(request: HttpRequest, workspace: str, key: str, draft: int | None = None) -> InstallationOut:
    """`draft=<id>` describes that draft's test copy instead (maintainers)."""
    inst = _installation(request, workspace, key)
    try:
        return _out(request, inst, _space(request, inst, draft))
    except services.Refused as exc:
        raise _refusal(exc) from exc


@router.patch("/workspace-apps/{workspace}/{key}", response=InstallationOut,
              summary="Switch an app on or off, or its public pages")
@gate("integrations", setting=True)
def update_workspace_app(request: HttpRequest, workspace: str, key: str, payload: InstallationPatch) -> InstallationOut:
    inst = _installation(request, workspace, key, enabled=False)
    _require_manage(request, workspace)
    if payload.enabled is not None:
        inst.enabled = payload.enabled
    if payload.public is not None:
        inst.public = payload.public
    inst.save()
    return _out(request, inst)


@router.delete("/workspace-apps/{workspace}/{key}", response={204: None},
               summary="Remove an app AND every record it holds")
@gate("integrations", setting=True)
def uninstall_workspace_app(request: HttpRequest, workspace: str, key: str):
    inst = _installation(request, workspace, key, enabled=False)
    _require_manage(request, workspace)
    inst.delete()
    return Status(204, None)


# --- the life cycle: drafts, changes, checks, publish, rollback -----------------------------


def _draft_out(v: AppVersion) -> DraftOut:
    return DraftOut(id=v.pk, number=v.number, origin=v.origin, template_version=v.template_version.version,
                    note=v.note, space=v.space, created_by=_email(v.created_by), created_at=v.created_at,
                    review=services.review(v))


def _open_draft(request: HttpRequest, workspace: str, key: str, *,
                helper_ok: bool = False) -> tuple[AppInstallation, AppVersion]:
    """The open draft, for a maintainer — or, with `helper_ok`, for an agent a
    maintainer asked to work on it (`AppVersion.helpers`): reading, changing,
    testing and checking it, never publishing or throwing it away."""
    inst = _installation(request, workspace, key, enabled=False)
    v = services.open_draft(inst)
    if not (helper_ok and v is not None and access.is_helper(request.user, v)):
        _require_maintain(request, inst)
    if v is None:
        raise HttpError(404, "this app has no open draft — start one first")
    return inst, v


@router.get("/workspace-apps/{workspace}/{key}/guide", response=GuideOut,
            summary="How to change this app for a person: the operations, and the app as it is now")
@gate("member")
def get_workspace_app_guide(request: HttpRequest, workspace: str, key: str) -> GuideOut:
    """Read this before adding changes. `manifest` is the open draft's composed
    manifest if there is one, else the live one."""
    inst = _installation(request, workspace, key, enabled=False)
    v = services.open_draft(inst)
    if not (v is not None and access.is_helper(request.user, v)):
        _require_maintain(request, inst)
    m = services.effective(v)[0] if v else inst.manifest
    return GuideOut(contract=guide.CONTRACT, manifest=m, draft=v.pk if v else None)


@router.get("/workspace-apps/{workspace}/{key}/versions", response=list[VersionOut],
            summary="Every version of an app, newest first")
@gate("member")
def list_workspace_app_versions(request: HttpRequest, workspace: str, key: str) -> list[VersionOut]:
    inst = _installation(request, workspace, key, enabled=False)
    _require_maintain(request, inst)
    return [VersionOut(number=v.number, status=v.status, origin=v.origin, template_version=v.template_version.version,
                       note=v.note, changes=len(v.overlay), created_by=_email(v.created_by), created_at=v.created_at,
                       published_by=_email(v.published_by), published_at=v.published_at)
            for v in inst.versions.select_related("template_version", "created_by", "published_by")]


@router.get("/workspace-apps/{workspace}/{key}/draft", response=DraftOut,
            summary="The app's open draft, with a plain-language review of what it changes")
@gate("member")
def get_workspace_app_draft(request: HttpRequest, workspace: str, key: str) -> DraftOut:
    return _draft_out(_open_draft(request, workspace, key, helper_ok=True)[1])


@router.post("/workspace-apps/{workspace}/{key}/draft", response={201: DraftOut},
             summary="Start a draft (a change, or the template's update) with its own test copy")
@gate("member", kind="content", viewer_write="an app's maintainer may be a viewer granted the role by name")
def start_workspace_app_draft(request: HttpRequest, workspace: str, key: str, payload: DraftIn) -> Status:
    """The draft starts as the live app. Its test copy holds the template's sample
    records — never live data — and sends nothing. `upgrade=true` puts the same
    changes over the newest template version."""
    inst = _installation(request, workspace, key, enabled=False)
    _require_maintain(request, inst)
    try:
        v = services.start_draft(inst, user=request.user, upgrade=payload.upgrade, note=payload.note)
    except services.Refused as exc:
        raise _refusal(exc) from exc
    return Status(201, _draft_out(v))


@router.delete("/workspace-apps/{workspace}/{key}/draft", response={204: None},
               summary="Throw a draft away, with its test copy")
@gate("member", kind="content", viewer_write="an app's maintainer may be a viewer granted the role by name")
def discard_workspace_app_draft(request: HttpRequest, workspace: str, key: str):
    services.discard_draft(_open_draft(request, workspace, key)[1])
    return Status(204, None)


@router.post("/workspace-apps/{workspace}/{key}/draft/ops", response={201: DraftOut},
             summary="Add one change to the app's draft (see the guide for the operations)")
@gate("member", kind="content", viewer_write="an app's maintainer may be a viewer granted the role by name")
def add_workspace_app_change(request: HttpRequest, workspace: str, key: str,
                             payload: Body[dict[str, Any]]) -> Status:
    """One change, as a JSON object: `op` names it, `intent` is the person's own
    words, the rest are the op's fields (see the guide). Validated against the
    draft as it stands: a change that cannot apply is refused with the reason
    (422) and the draft is unchanged."""
    _, v = _open_draft(request, workspace, key, helper_ok=True)
    try:
        services.add_op(v, payload, by=request.user.email)
    except services.Refused as exc:
        raise _refusal(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    v.refresh_from_db()
    return Status(201, _draft_out(v))


@router.delete("/workspace-apps/{workspace}/{key}/draft/ops/{op_id}", response=DraftOut,
               summary="Take one change out of the app's draft")
@gate("member", kind="content", viewer_write="an app's maintainer may be a viewer granted the role by name")
def remove_workspace_app_change(request: HttpRequest, workspace: str, key: str, op_id: str) -> DraftOut:
    _, v = _open_draft(request, workspace, key, helper_ok=True)
    try:
        services.remove_op(v, op_id)
    except services.Refused as exc:
        raise _refusal(exc) from exc
    v.refresh_from_db()
    return _draft_out(v)


@router.post("/workspace-apps/{workspace}/{key}/draft/checks", response=DraftOut,
             summary="Run the app's checks (its scenarios) against the draft")
@gate("member", kind="action", viewer_write="an app's maintainer may be a viewer granted the role by name")
def check_workspace_app_draft(request: HttpRequest, workspace: str, key: str) -> DraftOut:
    _, v = _open_draft(request, workspace, key, helper_ok=True)
    services.run_checks(v)
    return _draft_out(v)


@router.get("/workspace-apps/{workspace}/{key}/setup", response=SetupOut,
            summary="The app's first-run steps, and which are done")
@gate("member")
def get_workspace_app_setup(request: HttpRequest, workspace: str, key: str) -> SetupOut:
    inst = _installation(request, workspace, key, enabled=False)
    _require_maintain(request, inst)
    return SetupOut(**services.setup_status(inst))


@router.post("/workspace-apps/{workspace}/{key}/setup", response=SetupOut,
             summary="Tick a first-run step done by hand, or put the list away")
@gate("member", kind="content", viewer_write="an app's maintainer may be a viewer granted the role by name")
def update_workspace_app_setup(request: HttpRequest, workspace: str, key: str, payload: SetupIn) -> SetupOut:
    inst = _installation(request, workspace, key, enabled=False)
    _require_maintain(request, inst)
    if payload.step is not None:
        step = next((s for s in inst.manifest.get("setup") or [] if s["id"] == payload.step), None)
        if step is None or "manual" not in step["check"]:
            raise HttpError(422, "only a step the app cannot check for itself is ticked by hand")
        done = [s for s in inst.setup_done or [] if s != payload.step] + ([payload.step] if payload.done else [])
        inst.setup_done = done
    if payload.hidden is not None:
        inst.setup_hidden = payload.hidden
    inst.save(update_fields=["setup_done", "setup_hidden", "updated_at"])
    return SetupOut(**services.setup_status(inst))


def _helpers(request: HttpRequest, inst: AppInstallation) -> list[HelperOut]:
    draft = services.open_draft(inst)
    helping = set(draft.helpers.values_list("pk", flat=True)) if draft else set()
    agents = (Agent.objects.filter(workspace_id__in=perms.slugs_with(request.user, perms.READ), user__isnull=False)
              .select_related("user").order_by("name"))
    out = [HelperOut(slug=a.slug, name=a.name, home=a.workspace_id, login=a.user.email,
                     reachable=access.may_help(a.user, inst.workspace_id), helping=a.user_id in helping)
           for a in agents]
    return sorted(out, key=lambda h: (not h.reachable, h.home != inst.workspace_id, h.name.lower()))


@router.get("/workspace-apps/{workspace}/{key}/helpers", response=list[HelperOut],
            summary="Agents you could ask to change this app, and whether each can reach it")
@gate("member")
def list_workspace_app_helpers(request: HttpRequest, workspace: str, key: str) -> list[HelperOut]:
    """Every agent you can see whose login is an agent's own. `reachable` means its
    login is a member of this workspace; an agent that is not cannot touch the app
    until a workspace admin adds that login as a member."""
    inst = _installation(request, workspace, key, enabled=False)
    _require_maintain(request, inst)
    return _helpers(request, inst)


@router.post("/workspace-apps/{workspace}/{key}/draft/helpers", response=list[HelperOut],
             summary="Let an agent work on the open draft (change, test, check — never publish)")
@gate("member", kind="content", viewer_write="an app's maintainer may be a viewer granted the role by name")
def add_workspace_app_helper(request: HttpRequest, workspace: str, key: str, payload: HelperIn) -> list[HelperOut]:
    """For as long as the draft is open: published or thrown away, the access ends
    with it. The agent's login must already be a member of the workspace."""
    inst, v = _open_draft(request, workspace, key)
    agent = Agent.objects.filter(slug=payload.agent, user__isnull=False).select_related("user").first()
    if agent is None or not perms.can(request.user, agent.workspace_id, perms.READ):
        raise HttpError(404, f"agent '{payload.agent}' not found")
    if not access.may_help(agent.user, inst.workspace_id):
        raise HttpError(422, f"{agent.name} is not a member of this workspace: a workspace admin adds "
                             f"{agent.user.email} as a member first")
    v.helpers.add(agent.user)
    return _helpers(request, inst)


@router.post("/workspace-apps/{workspace}/{key}/draft/publish", response=InstallationOut,
             summary="Publish the draft: it becomes the live app")
@gate("member", kind="content", viewer_write="an app's maintainer may be a viewer granted the role by name")
def publish_workspace_app_draft(request: HttpRequest, workspace: str, key: str) -> InstallationOut:
    """Refused while a change is set aside or a check fails. A change to what the
    public sees or who can do what needs a workspace admin."""
    inst, v = _open_draft(request, workspace, key)
    try:
        services.publish(v, user=request.user, may_approve_risk=access.may_manage(request.user, workspace))
    except services.Refused as exc:
        raise _refusal(exc) from exc
    inst.refresh_from_db()
    return _out(request, inst)


@router.post("/workspace-apps/{workspace}/{key}/rollback", response=InstallationOut,
             summary="Make an earlier version live again (as a new version)")
@gate("member", kind="content", viewer_write="an app's maintainer may be a viewer granted the role by name")
def rollback_workspace_app(request: HttpRequest, workspace: str, key: str, payload: RollbackIn) -> InstallationOut:
    inst = _installation(request, workspace, key, enabled=False)
    _require_maintain(request, inst)
    try:
        services.rollback(inst, number=payload.number, user=request.user)
    except services.Refused as exc:
        raise _refusal(exc) from exc
    inst.refresh_from_db()
    return _out(request, inst)


# --- using an app -----------------------------------------------------------------------------


@router.get("/workspace-apps/{workspace}/{key}/query", response=list[dict],
            summary="Read records of one of an app's collections")
@gate("member")
def query_workspace_app(request: HttpRequest, workspace: str, key: str, collection: str, where: str = "",
                        order: str = "-created_at", limit: int = 100, draft: int | None = None,
                        as_public: bool = False) -> list[dict]:
    """`where` is a JSON object of field equalities (plus `id`). Needs the
    collection's `read` role. `as_public` reads as a visitor would (a maintainer
    previewing a public page in a draft)."""
    inst = _installation(request, workspace, key)
    try:
        space = _space(request, inst, draft)
        m = services.manifest_for(inst, space)
        return services.query(inst, roles=_roles(request, inst, space, m), collection=collection,
                              where=_where(where), order=order, limit=limit, space=space, manifest=m,
                              public=as_public, user=request.user)
    except services.Refused as exc:
        raise _refusal(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc


@router.post("/workspace-apps/{workspace}/{key}/actions/{action}", response=dict,
             summary="Run one of an app's actions (its only way to write)")
@gate("member", kind="content",
      viewer_write="an app action declares its own role; services.act refuses a caller without it")
def act_workspace_app(request: HttpRequest, workspace: str, key: str, action: str, payload: ActionIn,
                      draft: int | None = None, as_public: bool = False) -> dict:
    inst = _installation(request, workspace, key)
    try:
        space = _space(request, inst, draft)
        m = services.manifest_for(inst, space)
        roles = {"public"} if as_public else _roles(request, inst, space, m)
        if as_public and space == LIVE:
            raise HttpError(422, "as_public is for previewing a draft; use the public door for the live app")
        return services.act(inst, roles=roles, action=action, record_id=payload.record, input=payload.input,
                            user=request.user, client_ip=client_ip(request), space=space, manifest=m,
                            public=as_public, params=payload.params)
    except services.Refused as exc:
        raise _refusal(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc


@router.get("/workspace-apps/{workspace}/{key}/records/{record_id}/history", response=list[dict],
            summary="Every action that wrote one record, newest first")
@gate("member")
def workspace_app_record_history(request: HttpRequest, workspace: str, key: str, record_id: int,
                                 draft: int | None = None) -> list[dict]:
    inst = _installation(request, workspace, key)
    try:
        space = _space(request, inst, draft)
        return services.history(inst, roles=_roles(request, inst, space, services.manifest_for(inst, space)),
                                record_id=record_id, space=space, user=request.user)
    except services.Refused as exc:
        raise _refusal(exc) from exc


def _file_field(m: dict, collection: str, field: str, *, public: bool) -> dict:
    spec = (m["collections"].get(collection) or {}).get("fields", {}).get(field)
    if not spec or spec["type"] != "file":
        raise HttpError(422, f"'{collection}.{field}' is not a file field")
    if public and not any(a["role"] == "public" and a["collection"] == collection and field in a["input"]
                          and a.get("enabled", True) for a in m["actions"].values()):
        raise HttpError(404, "not found")
    return spec


@router.post("/workspace-apps/{workspace}/{key}/files", response={201: FileOut},
             summary="Upload a file for a file field (then pass its id to an action)")
@gate("member", kind="content", viewer_write="the action that uses the file checks the role; an unused upload is swept")
def upload_workspace_app_file(request: HttpRequest, workspace: str, key: str, collection: Form[str],
                              field: Form[str], file: File[UploadedFile], draft: int | None = None) -> Status:
    inst = _installation(request, workspace, key)
    try:
        space = _space(request, inst, draft)
        spec = _file_field(services.manifest_for(inst, space), collection, field, public=False)
        f = services.upload(inst, space=space, name=file.name, content_type=file.content_type, body=file.read(),
                            label=request.user.email, client_ip=client_ip(request), accept=spec["accept"],
                            max_mb=spec["max_mb"])
    except services.Refused as exc:
        raise _refusal(exc) from exc
    except services.TooManyRequests as exc:
        raise ProblemError(429, "Too many requests", type_=TYPE_RATE_LIMIT, detail="Too many uploads.") from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, FileOut(id=f.pk, name=f.name, size=f.size, content_type=f.content_type))


@router.get("/workspace-apps/{workspace}/{key}/files/{file_id}", response={200: None},
            summary="Download a file a record you can read points at")
@gate("member")
def download_workspace_app_file(request: HttpRequest, workspace: str, key: str, file_id: int,
                                draft: int | None = None):
    inst = _installation(request, workspace, key)
    try:
        space = _space(request, inst, draft)
        f = services.readable_file(inst, roles=_roles(request, inst, space, services.manifest_for(inst, space)),
                                   file_id=file_id, space=space, user=request.user)
    except services.Refused as exc:
        raise _refusal(exc) from exc
    response = HttpResponse(services.file_bytes(f), content_type=f.content_type)
    response["Content-Disposition"] = f'attachment; filename="{f.name}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


# --- role grants ------------------------------------------------------------------------------


def _grant_roles(inst: AppInstallation) -> set[str]:
    return set(inst.manifest.get("roles") or {}) | {access.MAINTAINER}


@router.get("/workspace-apps/{workspace}/{key}/grants", response=list[GrantOut],
            summary="App roles given to members by name")
@gate("members.manage")
def list_workspace_app_grants(request: HttpRequest, workspace: str, key: str) -> list[GrantOut]:
    inst = _installation(request, workspace, key, enabled=False)
    if not access.may_grant(request.user, workspace):
        raise HttpError(403, "granting app roles needs the members permission (admin)")
    return [GrantOut(id=g.pk, email=g.user.email, role=g.role, granted_at=g.granted_at)
            for g in inst.grants.select_related("user")]


@router.post("/workspace-apps/{workspace}/{key}/grants", response={201: GrantOut},
             summary="Give a workspace member one of an app's roles (or `maintainer`)")
@gate("members.manage", setting=True)
def add_workspace_app_grant(request: HttpRequest, workspace: str, key: str, payload: GrantIn) -> Status:
    inst = _installation(request, workspace, key, enabled=False)
    if not access.may_grant(request.user, workspace):
        raise HttpError(403, "granting app roles needs the members permission (admin)")
    if payload.role not in _grant_roles(inst):
        raise HttpError(422, f"'{payload.role}' is not a role of this app")
    user = User.objects.filter(email__iexact=payload.email.strip()).first()
    if user is None or not perms.can(user, workspace, perms.READ):
        raise HttpError(422, "app roles go only to members of the workspace")
    grant, _ = AppRoleGrant.objects.get_or_create(installation=inst, user=user, role=payload.role,
                                                  defaults={"granted_by": request.user})
    return Status(201, GrantOut(id=grant.pk, email=user.email, role=grant.role, granted_at=grant.granted_at))


@router.delete("/workspace-apps/{workspace}/{key}/grants/{grant_id}", response={204: None},
               summary="Take back an app role given by name")
@gate("members.manage", setting=True)
def delete_workspace_app_grant(request: HttpRequest, workspace: str, key: str, grant_id: int):
    inst = _installation(request, workspace, key, enabled=False)
    if not access.may_grant(request.user, workspace):
        raise HttpError(403, "granting app roles needs the members permission (admin)")
    inst.grants.filter(pk=grant_id).delete()
    return Status(204, None)


@router.post("/workspace-apps/{workspace}/{key}/actions/{action}/bulk", response=dict,
             summary="Run one of an app's bulk actions over many records (bulk reject)")
@gate("member", kind="content",
      viewer_write="an app action declares its own role; services.act refuses a caller without it")
def bulk_act_workspace_app(request: HttpRequest, workspace: str, key: str, action: str, payload: BulkActionIn,
                           draft: int | None = None) -> dict:
    inst = _installation(request, workspace, key)
    try:
        space = _space(request, inst, draft)
        m = services.manifest_for(inst, space)
        return services.act_bulk(inst, roles=_roles(request, inst, space, m), action=action,
                                 record_ids=payload.records, input=payload.input, params=payload.params,
                                 user=request.user, space=space, manifest=m)
    except services.Refused as exc:
        raise _refusal(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc


@router.post("/workspace-apps/{workspace}/{key}/links/{link}/{record_id}", response=LinkOut,
             summary="Make a record's private link (to send a candidate by hand)")
@gate("member", kind="content", viewer_write="needs the app role that reads the link's collection",
      step_up="the link opens only the fields the link declares on one record the caller can already read, "
              "and its actions are the link's own — it grants nothing the caller's session doesn't")
def make_workspace_app_link(request: HttpRequest, workspace: str, key: str, link: str, record_id: int,
                            draft: int | None = None) -> LinkOut:
    inst = _installation(request, workspace, key)
    space = _space(request, inst, draft)
    m = services.manifest_for(inst, space)
    spec = (m.get("links") or {}).get(link)
    if spec is None:
        raise HttpError(404, f"no link '{link}'")
    roles = _roles(request, inst, space, m)
    if not services.may_read(m["collections"][spec["collection"]], roles):
        raise HttpError(403, f"making this link needs the role that reads '{spec['collection']}'")
    rec = inst.records.filter(space=space, collection=spec["collection"], pk=record_id).first()
    sc = services.scope_for(inst, m, space, request.user, roles)
    if rec is None or (sc and not sc.sees(rec.collection, rec.pk)):
        raise HttpError(404, f"no {spec['collection']} {record_id}")
    token = services.mint_link(inst, link, rec)
    return LinkOut(url=services.link_url(inst, token), token=token, days=spec["days"])


@router.get("/workspace-apps/{workspace}/{key}/retention", response=list[dict],
            summary="What the app's retention rules would anonymize or delete now")
@gate("member")
def preview_workspace_app_retention(request: HttpRequest, workspace: str, key: str) -> list[dict]:
    inst = _installation(request, workspace, key)
    _require_maintain(request, inst)
    return services.retention_preview(inst)


@router.post("/workspace-apps/{workspace}/{key}/retention", response=dict,
             summary="Run the app's retention rules now (the clock also runs them daily)")
@gate("integrations", setting=True)
def apply_workspace_app_retention(request: HttpRequest, workspace: str, key: str) -> dict:
    inst = _installation(request, workspace, key)
    _require_manage(request, workspace)
    return services.retention_apply(inst)


@router.get("/workspace-apps/{workspace}/{key}/subject-export", response=dict,
            summary="Everything the app holds about one person, by email (a data-subject access request)")
@gate("integrations")
def export_workspace_app_subject(request: HttpRequest, workspace: str, key: str, email: str) -> dict:
    inst = _installation(request, workspace, key)
    _require_manage(request, workspace)
    try:
        return services.subject_export(inst, email=email)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc


# --- the public door -----------------------------------------------------------------------
# `auth=None`, and `/api/public-apps/` is allowlisted in apps/common/middleware.py.
# Every route answers 404 alike for "no such workspace", "no such app", "switched
# off" and "not public", so the door reveals nothing a visitor was not shown.


def _public(workspace: str, key: str) -> AppInstallation:
    inst = (AppInstallation.objects.select_related("workspace")
            .filter(workspace_id=workspace, key=key, enabled=True, public=True).first())
    if inst is None or not any(p["audience"] == "public" for p in inst.manifest["pages"]):
        raise HttpError(404, "not found")
    return inst


def _public_schema(m: dict) -> dict:
    """Only what a visitor's pages need: the public fields, and the public forms' inputs."""
    full = _schema(m)
    out = {}
    for name, c in m["collections"].items():
        visible = set((c.get("public") or {}).get("fields") or [])
        visible |= {f for a in m["actions"].values() if a["role"] == "public" and a["collection"] == name
                    and a.get("enabled", True) for f in a["input"]}
        if visible:
            out[name] = {**full[name], "fields": {k: v for k, v in full[name]["fields"].items() if k in visible}}
    out["_actions"] = {k: v for k, v in full["_actions"].items() if v["role"] == "public" and v["enabled"]}
    out["_feeds"] = {name: c["public"]["feed"]["page"] for name, c in m["collections"].items()
                     if (c.get("public") or {}).get("feed")}
    return out


@public_router.get("/public-apps/{workspace}/{key}", response=PublicAppOut, auth=None,
                   summary="An app's public pages (anonymous)")
@gate("anonymous")
def get_public_app(request: HttpRequest, workspace: str, key: str) -> PublicAppOut:
    inst = _public(workspace, key)
    ws: Workspace = inst.workspace
    m = inst.manifest
    return PublicAppOut(key=inst.key, name=inst.name, workspace=ws.slug, workspace_name=ws.display_name,
                        pages=[PageOut(**p) for p in m["pages"] if p["audience"] == "public"],
                        data_model=_public_schema(m), settings=_settings(m))


@public_router.get("/public-apps/{workspace}/{key}/query", response=list[dict], auth=None,
                   summary="Read an app collection's public projection (anonymous)")
@gate("anonymous")
def query_public_app(request: HttpRequest, workspace: str, key: str, collection: str, where: str = "",
                     order: str = "-created_at", limit: int = 100) -> list[dict]:
    inst = _public(workspace, key)
    try:
        return services.query(inst, roles={"public"}, collection=collection, where=_where(where), order=order,
                              limit=limit, public=True)
    except services.Refused as exc:
        raise _refusal(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc


@public_router.post("/public-apps/{workspace}/{key}/actions/{action}", response=dict, auth=None,
                    summary="Submit one of an app's public forms (anonymous, rate-limited)")
@gate("anonymous", kind="request")  # a form a visitor submits for the workspace to decide on; grants nothing
def act_public_app(request: HttpRequest, workspace: str, key: str, action: str, payload: ActionIn) -> dict:
    inst = _public(workspace, key)
    if payload.input.pop("website", None):  # the honeypot: a bot filled the hidden field
        return {"id": 0}
    try:
        return services.act(inst, roles={"public"}, action=action, record_id=None, input=payload.input,
                            user=request.user, client_ip=client_ip(request), public=True)
    except services.TooManyRequests as exc:
        raise ProblemError(429, "Too many requests", type_=TYPE_RATE_LIMIT,
                           detail="Too many submissions from this address. Try again later.") from exc
    except services.Refused as exc:
        raise _refusal(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc


@public_router.post("/public-apps/{workspace}/{key}/files", response={201: FileOut}, auth=None,
                    summary="Upload a file for a public form's file field (anonymous, rate-limited)")
@gate("anonymous", kind="request")  # bytes for a public form's file field; unread until a form claims them
def upload_public_app_file(request: HttpRequest, workspace: str, key: str, collection: Form[str],
                           field: Form[str], file: File[UploadedFile]) -> Status:
    inst = _public(workspace, key)
    spec = _file_field(inst.manifest, collection, field, public=True)
    try:
        f = services.upload(inst, space=LIVE, name=file.name, content_type=file.content_type, body=file.read(),
                            label="", client_ip=client_ip(request), accept=spec["accept"], max_mb=spec["max_mb"])
    except services.TooManyRequests as exc:
        raise ProblemError(429, "Too many requests", type_=TYPE_RATE_LIMIT, detail="Too many uploads.") from exc
    except services.Refused as exc:
        raise _refusal(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, FileOut(id=f.pk, name=f.name, size=f.size, content_type=f.content_type))



@public_router.get("/public-apps/{workspace}/{key}/feed/{collection}.json", response=list[dict], auth=None,
                   summary="A public collection's schema.org JobPosting feed (anonymous)")
@gate("anonymous")
def feed_public_app(request: HttpRequest, workspace: str, key: str, collection: str) -> list[dict]:
    inst = _public(workspace, key)
    try:
        return services.feed(inst, collection)
    except services.Refused as exc:
        raise _refusal(exc) from exc


@public_router.get("/public-apps/{workspace}/{key}/feed/{collection}.xml", response={200: None}, auth=None,
                   summary="The same feed as XML, for job boards that pull one (anonymous)")
@gate("anonymous")
def feed_xml_public_app(request: HttpRequest, workspace: str, key: str, collection: str):
    inst = _public(workspace, key)
    try:
        return HttpResponse(services.feed_xml(inst, collection), content_type="application/xml; charset=utf-8")
    except services.Refused as exc:
        raise _refusal(exc) from exc


# --- the link door ---------------------------------------------------------------------------
# A private link names ONE record (services.mint_link). Its holder needs no
# account: the signed token is the credential. It works while the app is
# switched on, public pages or not — a candidate answers an offer whether or not
# the careers page is live. Every failure is the same 404.


def _link(workspace: str, key: str, token: str) -> tuple[AppInstallation, dict]:
    inst = (AppInstallation.objects.select_related("workspace")
            .filter(workspace_id=workspace, key=key, enabled=True).first())
    if inst is None:
        raise HttpError(404, "this link is not valid")
    try:
        return inst, services.verify_link(inst, token)
    except services.Refused as exc:
        raise _refusal(exc) from exc


@public_router.get("/public-apps/{workspace}/{key}/link/{token}", response=LinkPageOut, auth=None,
                   summary="The page behind a private link (anonymous; the token is the credential)")
@gate("anonymous")
def get_link_page(request: HttpRequest, workspace: str, key: str, token: str) -> LinkPageOut:
    inst, link = _link(workspace, key, token)
    view = services.link_view(inst, link)
    ws: Workspace = inst.workspace
    return LinkPageOut(key=inst.key, name=inst.name, workspace=ws.slug, workspace_name=ws.display_name,
                       link=view["link"], label=view["label"], record=view["record"], page=PageOut(**view["page"]),
                       actions=view["actions"], data_model=view["data_model"], settings=_settings(link["manifest"]),
                       draft=view["draft"])


@public_router.post("/public-apps/{workspace}/{key}/link/{token}/actions/{action}", response=dict, auth=None,
                    summary="Run a link action on the link's own record (anonymous, rate-limited)")
@gate("anonymous", kind="request")  # changes only the one record the signed link names, through declared fields
def act_link_page(request: HttpRequest, workspace: str, key: str, token: str, action: str,
                  payload: ActionIn) -> dict:
    inst, link = _link(workspace, key, token)
    try:
        return services.act(inst, roles={"link"}, action=action, record_id=link["record"], input=payload.input,
                            user=request.user, client_ip=client_ip(request), space=link["space"],
                            manifest=link["manifest"], link=link)
    except services.TooManyRequests as exc:
        raise ProblemError(429, "Too many requests", type_=TYPE_RATE_LIMIT, detail="Too many requests.") from exc
    except services.Refused as exc:
        raise _refusal(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
