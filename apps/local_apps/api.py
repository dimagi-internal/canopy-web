"""Local apps over REST (and so MCP): the store section, publishing, installing, and
what a runner asks for a run. Design: docs/architecture/local-apps.md.

* A user app belongs to the person, not a workspace: any signed-in person can see and
  install one, for themselves, on their own laptop. The agent they choose decides
  which workspace its sessions land in.
* Releasing goes through the `local_app.release` approval; revoking is a superuser.
* A runner reaches only its owner's installations on itself, under the runner
  protocol's own path, so `runner_auth` needed no new route.
"""

from __future__ import annotations

import uuid

from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents import access as agent_access
from apps.agents import turn_context
from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import refusals
from apps.api.route_gates import gate
from apps.contacts import people
from apps.harness.runner_queries import runner_or_404
from apps.tokens import kinds
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.links import scoped_url
from apps.workspaces.models import Workspace

from . import services, trust
from .models import LocalAppInstallation, LocalAppMenuChoice, LocalAppRun, LocalAppSubmission
from .schemas import (
    LocalAppAcceptIn,
    LocalAppBeatIn,
    LocalAppBeatOut,
    LocalAppCanopyCallIn,
    LocalAppCanopyCallOut,
    LocalAppConfigureIn,
    LocalAppDetailOut,
    LocalAppFinishIn,
    LocalAppInstallationOut,
    LocalAppInstallIn,
    LocalAppMenuAgentsOut,
    LocalAppMenuChoiceIn,
    LocalAppMenuOut,
    LocalAppReviewOut,
    LocalAppRevokeIn,
    LocalAppRunOut,
    LocalAppStartRunIn,
    LocalAppStartRunOut,
    LocalAppStoreOut,
    LocalAppSubmissionOut,
    LocalAppSubmitIn,
    RunnerLocalAppsOut,
)

router = Router(auth=session_auth, tags=["local-apps"])
#: A menu item lists a few agents to tap, not a directory.
MENU_MAX_AGENTS = 12
runner_router = Router(auth=session_auth, tags=["local-apps"])


def _workspace_or_404(request: HttpRequest, slug: str) -> Workspace:
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or not perms.can(request.user, ws, perms.READ):
        raise HttpError(404, "workspace not found")
    return ws


def _usable_agents(user, only: set[tuple[str, str]] | None = None) -> list[dict[str, str]]:
    """The agents this person may use, across their workspaces. `only` narrows it to
    those (workspace, slug) pairs, so a short chosen list is checked without the rest."""
    qs = Agent.objects.filter(workspace__in=perms.slugs_with(user, perms.READ), user__isnull=False)
    if only is not None:
        if not only:
            return []
        wanted = Q()
        for workspace, slug in only:
            wanted |= Q(workspace_id=workspace, slug=slug)
        qs = qs.filter(wanted)
    rows = list(qs.order_by("name", "slug"))
    standings = agent_access.Standings([user], rows)
    return [
        {"slug": a.slug, "name": a.name or a.slug, "workspace": a.workspace_id}
        for a in rows
        if not turn_context.for_asker(a, user, verified=False, standing=standings.of(user, a)).refused
    ]


def _the_person_themself(request: HttpRequest) -> None:
    """These decide what a person agreed to and where their conversation goes. An agent
    acting for them could otherwise accept a wider disclosure or redirect the transcript."""
    if kinds.speaks_for_a_turn(request) or people.agent_of_login(request.user) is not None:
        raise HttpError(403, "Only the person may do this")


def _refused(exc: services.Refused) -> HttpError:
    return HttpError(exc.status, str(exc))


# --- the store --------------------------------------------------------------------------


@router.get("/store/", response=LocalAppStoreOut, summary="User apps: apps you install for yourself")
@gate("authenticated")
def list_local_app_store(request: HttpRequest) -> LocalAppStoreOut:
    mine = set(LocalAppInstallation.objects.filter(user=request.user).values_list("template__key", flat=True))
    apps = [
        {
            "key": s.key,
            "name": s.manifest["name"],
            "description": s.manifest["description"],
            "version": s.version,
            "installed": s.key in mine,
        }
        for s in services.catalog()
    ]
    return LocalAppStoreOut(apps=apps)


@router.get(
    "/store/apps/{key}",
    response=LocalAppDetailOut,
    summary="One local app, as someone deciding to install it",
)
@gate("authenticated")
def get_local_app(request: HttpRequest, key: str) -> LocalAppDetailOut:
    sub = services.latest(key)
    if sub is None:
        raise HttpError(404, "no such app")
    m = sub.manifest
    versions = LocalAppSubmission.objects.filter(key=key, released_at__isnull=False)
    return LocalAppDetailOut(
        key=sub.key,
        name=m["name"],
        description=m["description"],
        version=sub.version,
        publisher="canopy",
        reviewed=sub.reviewed,
        notice=m["notice"],
        disclosure=m["disclosure"],
        settings=m["settings"],
        presentation=m.get("presentation") or {},
        digest=trust.digest(m),
        agents=_usable_agents(request.user),
        computers=[{"id": str(r.pk), "name": r.name} for r in services.computers_of(request.user)],
        versions=[
            {
                "version": v.version,
                "released_at": v.released_at.isoformat(),
                "changelog": v.manifest["changelog"],
                "revoked": bool(v.revoked_at),
            }
            for v in versions[:10]
        ],
    )


# --- installing -------------------------------------------------------------------------


def _run_out(run: LocalAppRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": str(run.pk),
        "version": run.version.version,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "state": run.state,
        "counts": run.counts,
        "stop_reason": run.stop_reason,
        "stop_requested": run.stop_requested,
        "running": services.live_run(run.installation) is not None and run.finished_at is None,
    }


def _installation_out(inst: LocalAppInstallation, user) -> LocalAppInstallationOut:
    state = services.resolve(inst)
    top = services.latest(inst.template.key) or inst.accepted
    version = state["version"]
    return LocalAppInstallationOut(
        id=inst.pk,
        key=inst.template.key,
        name=inst.template.name,
        workspace=inst.workspace_id,
        computer={"id": str(inst.runner_id), "name": inst.runner.name},
        settings=inst.settings,
        settings_spec=top.manifest["settings"],
        agents=_usable_agents(user),
        version=version.version if version else None,
        needs_acceptance=state["needs_acceptance"],
        changes=state["changes"],
        digest=trust.digest(top.manifest),
        refusal=state["refusal"],
        last_run=_run_out(inst.runs.select_related("version", "installation").first()),
    )


def _my_installation(request: HttpRequest, installation_id: int) -> LocalAppInstallation:
    inst = (
        LocalAppInstallation.objects.select_related("template", "runner", "workspace", "accepted")
        .filter(pk=installation_id, user=request.user)
        .first()
    )
    if inst is None:
        raise HttpError(404, "no such installation")
    return inst


@router.post(
    "/store/apps/{key}/install",
    response={201: LocalAppInstallationOut},
    summary="Install a local app on one of your computers",
)
@gate("self", setting=True)
def install_local_app(request: HttpRequest, key: str, payload: LocalAppInstallIn):
    """Send back the `digest` you were shown. A web page can install and configure an
    app, but only the person at the computer can start one."""
    _the_person_themself(request)
    try:
        inst = services.install(
            key=key,
            user=request.user,
            runner_id=payload.runner_id,
            settings=payload.settings,
            accept_digest=payload.accept_digest,
        )
    except services.Refused as exc:
        raise _refused(exc) from exc
    return Status(201, _installation_out(inst, request.user))


@router.get(
    "/installations/",
    response=list[LocalAppInstallationOut],
    summary="Your installed local apps, on all your computers",
)
@gate("self")
def list_my_local_apps(request: HttpRequest) -> list[LocalAppInstallationOut]:
    rows = LocalAppInstallation.objects.filter(user=request.user).select_related(
        "template", "runner", "workspace", "accepted"
    )
    return [_installation_out(i, request.user) for i in rows]


@router.get("/menu/", response=LocalAppMenuOut, summary="What your apps add to your account menu")
@gate("self")
def list_local_app_menu(request: HttpRequest) -> LocalAppMenuOut:
    """Items your installed apps add to the account menu, the ones you turned on in each
    app's settings. An item that asks for an agent comes with the agents you may use."""
    return _menu_out(request.user)


def _menu_out(user) -> LocalAppMenuOut:
    """Each item with the agents it lists. A chosen list checks only those agents, which
    is what keeps the menu quick for someone who can use many; an unchosen one lists all."""
    choices = {(c.app, c.item): c.agents for c in LocalAppMenuChoice.objects.filter(user=user)}
    everyone: list[dict[str, str]] | None = None
    out = []
    for inst, item in services.menu_items(user):
        ref = (inst.template.key, item["key"])
        agents: list[dict[str, str]] = []
        if item["pick"] == "agent":
            if ref in choices:
                wanted = [(a["workspace"], a["slug"]) for a in choices[ref]]
                usable = {(a["workspace"], a["slug"]): a for a in _usable_agents(user, only=set(wanted))}
                agents = [usable[w] for w in wanted if w in usable]
            else:
                if everyone is None:
                    everyone = _usable_agents(user)
                agents = everyone
        out.append(
            {
                "app": ref[0],
                "app_name": inst.template.name,
                "key": ref[1],
                "label": item["label"],
                "path": item["path"],
                "pick": item["pick"],
                "agents": agents,
                "chosen": ref in choices,
            }
        )
    return LocalAppMenuOut(items=out)


@router.get("/menu/agents/", response=LocalAppMenuAgentsOut, summary="Agents you may list in your account menu")
@gate("self")
def list_local_app_menu_agents(request: HttpRequest) -> LocalAppMenuAgentsOut:
    """Every agent you may use, to choose which ones an account-menu item lists."""
    return LocalAppMenuAgentsOut(agents=_usable_agents(request.user))


@router.put("/menu/{app}/{item}/agents", response=LocalAppMenuOut, summary="Choose the agents a menu item lists")
@gate("self", setting=True)
def set_local_app_menu_agents(
    request: HttpRequest, app: str, item: str, payload: LocalAppMenuChoiceIn
) -> LocalAppMenuOut:
    """Which agents one of your account-menu items lists, in order. An empty list goes back
    to every agent you may use."""
    _the_person_themself(request)
    entry = next(
        (i for inst, i in services.menu_items(request.user) if (inst.template.key, i["key"]) == (app, item)), None
    )
    if entry is None:
        raise HttpError(404, "that item isn't in your menu")
    if entry["pick"] != "agent":
        raise HttpError(422, "that item doesn't list agents")
    wanted = list(dict.fromkeys((a.workspace, a.slug) for a in payload.agents))
    if len(wanted) > MENU_MAX_AGENTS:
        raise HttpError(422, f"choose at most {MENU_MAX_AGENTS} agents")
    if not wanted:
        LocalAppMenuChoice.objects.filter(user=request.user, app=app, item=item).delete()
        return _menu_out(request.user)
    usable = {(a["workspace"], a["slug"]) for a in _usable_agents(request.user, only=set(wanted))}
    if missing := [f"{ws}/{slug}" for ws, slug in wanted if (ws, slug) not in usable]:
        raise HttpError(403, f"you can't use: {', '.join(missing)}")
    LocalAppMenuChoice.objects.update_or_create(
        user=request.user,
        app=app,
        item=item,
        defaults={"agents": [{"workspace": ws, "slug": slug} for ws, slug in wanted]},
    )
    return _menu_out(request.user)


@router.patch("/installations/{installation_id}", response=LocalAppInstallationOut, summary="Configure a local app")
@gate("self", setting=True)
def configure_local_app(
    request: HttpRequest, installation_id: int, payload: LocalAppConfigureIn
) -> LocalAppInstallationOut:
    _the_person_themself(request)
    inst = _my_installation(request, installation_id)
    try:
        return _installation_out(services.configure(inst, payload.settings, request.user), request.user)
    except services.Refused as exc:
        raise _refused(exc) from exc


@router.post(
    "/installations/{installation_id}/accept",
    response=LocalAppInstallationOut,
    summary="Accept what a new version of a local app reads and sends",
)
@gate("self", setting=True)
def accept_local_app_changes(
    request: HttpRequest, installation_id: int, payload: LocalAppAcceptIn
) -> LocalAppInstallationOut:
    _the_person_themself(request)
    inst = _my_installation(request, installation_id)
    try:
        return _installation_out(services.accept(inst, payload.digest), request.user)
    except services.Refused as exc:
        raise _refused(exc) from exc


@router.delete("/installations/{installation_id}", response={204: None}, summary="Remove a local app from a computer")
@gate("self", setting=True)
def remove_local_app(request: HttpRequest, installation_id: int):
    """A running copy is told to stop at its next heartbeat, which now answers 404. The
    local copy goes when the runner next lists its apps and it isn't running."""
    _my_installation(request, installation_id).delete()
    return Status(204, None)


@router.post("/runs/{run_id}/stop", response=LocalAppRunOut, summary="Stop a running local app from the website")
@gate("self", kind="action")
def stop_local_app_run(request: HttpRequest, run_id: uuid.UUID) -> LocalAppRunOut:
    """Read at the app's next heartbeat, at most about 15 seconds later on a computer
    that's online. There is no way to start one from here."""
    run = (
        LocalAppRun.objects.select_related("installation", "version")
        .filter(pk=run_id, installation__user=request.user)
        .first()
    )
    if run is None:
        raise HttpError(404, "no such run")
    run.stop_requested = True
    run.save(update_fields=["stop_requested"])
    return LocalAppRunOut(**_run_out(run))


# --- publishing -------------------------------------------------------------------------


def _submission_out(sub: LocalAppSubmission) -> dict:
    return {
        "id": sub.pk,
        "key": sub.key,
        "version": sub.version,
        "sha256": sub.sha256,
        "status": sub.status,
        "approval_id": sub.approval_id,
        "review_url": scoped_url(sub.workspace, f"/apps/personal/review/{sub.pk}"),
    }


@router.post(
    "/submissions/",
    response={201: LocalAppSubmissionOut, 200: LocalAppSubmissionOut, **refusals(422)},
    summary="Submit a local app version for review",
)
@gate("content.write", kind="request")
def submit_local_app(request: HttpRequest, payload: LocalAppSubmitIn):
    """A package from `canopy-runner app pack`, base64-encoded. Nothing runs on the
    server. Files an approval for a superuser; nothing reaches anyone until it's
    approved. The same version with the same bytes returns the same submission.
    A package that is not a valid one (not a zip, no manifest, a bad version) is a 422."""
    if payload.workspace:
        ws = _workspace_or_404(request, payload.workspace)
    else:
        ws = wsvc.creation_workspace(request)
        if ws is None:
            raise HttpError(422, "say which workspace this is submitted from")
    if not perms.can(request.user, ws, perms.CONTENT_WRITE):
        raise HttpError(403, "you can't submit from this workspace")
    try:
        sub, created = services.submit(
            package_b64=payload.package_b64, note=payload.note, workspace=ws, user=request.user
        )
    except services.Refused as exc:
        raise _refused(exc) from exc
    return Status(201 if created else 200, LocalAppSubmissionOut(**_submission_out(sub)))


def _may_read_submission(request: HttpRequest, sub: LocalAppSubmission) -> bool:
    return bool(request.user.is_superuser or sub.submitted_by_id == request.user.pk)


@router.get("/submissions/", response=list[LocalAppSubmissionOut], summary="Local app versions, newest first")
@gate("superuser", "self")
def list_local_app_submissions(request: HttpRequest) -> list[LocalAppSubmissionOut]:
    """Yours; a superuser sees all, to review and revoke."""
    rows = LocalAppSubmission.objects.select_related("workspace", "approval")
    if not request.user.is_superuser:
        rows = rows.filter(submitted_by=request.user)
    return [LocalAppSubmissionOut(**_submission_out(s)) for s in rows[:100]]


@router.get(
    "/submissions/{submission_id}", response=LocalAppReviewOut, summary="One submission: its code changes and facts"
)
@gate("superuser", "self")
def get_local_app_submission(request: HttpRequest, submission_id: int) -> LocalAppReviewOut:
    """For its reviewer or its author. `diff` is against the last released version."""
    sub = (
        LocalAppSubmission.objects.select_related("workspace", "approval", "submitted_by")
        .filter(pk=submission_id)
        .first()
    )
    if sub is None or not _may_read_submission(request, sub):
        raise HttpError(404, "no such submission")
    return LocalAppReviewOut(
        **_submission_out(sub),
        name=sub.manifest["name"],
        size=sub.size,
        note=sub.note,
        submitted_by=sub.submitted_by.email if sub.submitted_by else "",
        submitted_at=sub.submitted_at.isoformat(),
        manifest=sub.manifest,
        facts=sub.facts,
        revoked_reason=sub.revoked_reason,
        can_revoke=bool(request.user.is_superuser and sub.released_at and not sub.revoked_at),
        **services.review(sub),
    )


@router.post(
    "/submissions/{submission_id}/revoke", response=LocalAppSubmissionOut, summary="Stop a released version everywhere"
)
@gate("superuser", setting=True)
def revoke_local_app(request: HttpRequest, submission_id: int, payload: LocalAppRevokeIn) -> LocalAppSubmissionOut:
    """Start refuses it, Canopy calls for its runs are refused at once, and running
    copies are told to stop at their next heartbeat. The reason is shown to people."""
    if not request.user.is_superuser:
        raise HttpError(403, "only a Canopy superuser can revoke a version")
    sub = LocalAppSubmission.objects.select_related("workspace", "approval").filter(pk=submission_id).first()
    if sub is None:
        raise HttpError(404, "no such submission")
    if not payload.reason.strip():
        raise HttpError(422, "say why: people whose copy stops are shown this")
    try:
        services.revoke(sub, reason=payload.reason.strip(), user=request.user)
    except services.Refused as exc:
        raise _refused(exc) from exc
    return LocalAppSubmissionOut(**_submission_out(sub))


# --- a runner's calls -------------------------------------------------------------------


def _own_runner(request: HttpRequest, runner_id: uuid.UUID):
    """The runner, called by its owner or its own token. Another person who can see or
    administer this box can't reach its owner's apps."""
    runner = runner_or_404(request, runner_id)
    if runner.owner_id != request.user.pk:
        raise HttpError(404, "runner not found")
    return runner


def _agent_name(inst: LocalAppInstallation, version: LocalAppSubmission) -> str | None:
    slug = services.agent_slug(inst, version)
    if not slug:
        return None
    agent = Agent.objects.filter(slug=slug).first()
    return (agent.name or agent.slug) if agent else slug


def _runner_installations(runner):
    return LocalAppInstallation.objects.filter(runner=runner, user_id=runner.owner_id).select_related(
        "template", "accepted", "workspace", "user", "runner"
    )


def _runner_installation(runner, key: str) -> LocalAppInstallation:
    inst = _runner_installations(runner).filter(template__key=key).first()
    if inst is None:
        raise HttpError(404, "that app isn't installed here")
    return inst


def _runner_run(runner, run_id: uuid.UUID) -> LocalAppRun:
    run = (
        LocalAppRun.objects.select_related(
            "installation__workspace", "installation__user", "installation__template", "version"
        )
        .filter(pk=run_id, installation__runner=runner)
        .first()
    )
    if run is None:
        raise HttpError(404, "no such run")
    return run


@runner_router.get(
    "/{runner_id}/local-apps/", response=RunnerLocalAppsOut, summary="This computer's installed local apps"
)
@gate("runner")
def list_runner_local_apps(request: HttpRequest, runner_id: uuid.UUID) -> RunnerLocalAppsOut:
    """Each app's version to run at its next Start, and why it can't start if it can't."""
    runner = _own_runner(request, runner_id)
    apps = []
    for inst in _runner_installations(runner):
        state = services.resolve(inst)
        version = state["version"]
        m = version.manifest if version else inst.accepted.manifest
        apps.append(
            {
                "key": inst.template.key,
                "name": m["name"],
                "version": version.version if version else None,
                "sha256": version.sha256 if version else None,
                "size": version.size if version else None,
                "notice": m["notice"],
                "disclosure": m["disclosure"],
                "entry": m["entry"],
                "max_minutes": m["max_minutes"],
                "settings": inst.settings,
                "needs_acceptance": state["needs_acceptance"],
                "changes": state["changes"],
                "refusal": state["refusal"],
                "running": services.live_run(inst) is not None,
                "agent": _agent_name(inst, version) if version else None,
                "checks": m["checks"],
                # Copy from the version that will run; a manifest from before it existed has none.
                "presentation": m.get("presentation") or {},
            }
        )
    return RunnerLocalAppsOut(apps=apps)


@runner_router.get(
    "/{runner_id}/local-apps/{key}/package", response={200: None}, summary="A local app version's package"
)
@gate("runner")
def get_runner_local_app_package(request: HttpRequest, runner_id: uuid.UUID, key: str, version: str):
    """Only the version this installation would run now. The runner checks the bytes
    against the sha256 from the list."""
    runner = _own_runner(request, runner_id)
    state = services.resolve(_runner_installation(runner, key))
    if state["version"] is None or state["version"].version != version:
        raise HttpError(404, "that version isn't one this computer may run")
    return HttpResponse(services.package_bytes(state["version"]), content_type="application/zip")


@runner_router.post(
    "/{runner_id}/local-apps/{key}/runs/", response={201: LocalAppStartRunOut}, summary="Start a local app run"
)
@gate("runner", kind="runner-protocol")
def start_runner_local_app(request: HttpRequest, runner_id: uuid.UUID, key: str, payload: LocalAppStartRunIn):
    """The last word on whether it may start: revoked, switched off, unaccepted and an
    agent the person can no longer use are all refused here."""
    runner = _own_runner(request, runner_id)
    try:
        run = services.start_run(_runner_installation(runner, key), payload.version)
    except services.Refused as exc:
        raise _refused(exc) from exc
    return Status(201, LocalAppStartRunOut(run_id=str(run.pk)))


@runner_router.post(
    "/{runner_id}/local-apps/runs/{run_id}/heartbeat",
    response=LocalAppBeatOut,
    summary="A running local app reports in",
)
@gate("runner", kind="runner-protocol")
def beat_runner_local_app(
    request: HttpRequest, runner_id: uuid.UUID, run_id: uuid.UUID, payload: LocalAppBeatIn
) -> LocalAppBeatOut:
    run = _runner_run(_own_runner(request, runner_id), run_id)
    return LocalAppBeatOut(**services.heartbeat(run, state=payload.state, counts=payload.counts))


@runner_router.post(
    "/{runner_id}/local-apps/runs/{run_id}/finish", response={204: None}, summary="A local app run ended"
)
@gate("runner", kind="runner-protocol")
def finish_runner_local_app(request: HttpRequest, runner_id: uuid.UUID, run_id: uuid.UUID, payload: LocalAppFinishIn):
    run = _runner_run(_own_runner(request, runner_id), run_id)
    services.finish(run, reason=payload.reason, counts=payload.counts)
    return Status(204, None)


@runner_router.post(
    "/{runner_id}/local-apps/runs/{run_id}/canopy",
    response=LocalAppCanopyCallOut,
    summary="A Canopy call for a running local app",
)
@gate("runner", kind="runner-protocol")
def call_canopy_for_local_app(
    request: HttpRequest, runner_id: uuid.UUID, run_id: uuid.UUID, payload: LocalAppCanopyCallIn
) -> LocalAppCanopyCallOut:
    """Only ops the manifest declares. The agent comes from the person's installation,
    and a send reaches only a session this run opened. Refusals arrive as `status`, so
    an app handles them; they never become anything more than that."""
    run = _runner_run(_own_runner(request, runner_id), run_id)
    return LocalAppCanopyCallOut(**services.canopy_call(run, payload.op, payload.args))
