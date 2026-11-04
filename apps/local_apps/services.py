"""Submit, release, revoke; install and configure; and what a run may do.

The rule that keeps this safe: the runner's own token never gains a general way to
act as its owner. Each Canopy call here is allowed by an installation the person
made while signed in, names its agent through that row, and reaches only sessions
the run itself opened.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import uuid

from django.db import transaction
from django.utils import timezone

from apps.agents import access as agent_access
from apps.agents.models import Agent
from apps.approvals import services as approvals
from apps.approvals.models import ApprovalRequest
from apps.canopy_sessions import attachment_storage, doors
from apps.canopy_sessions import services as session_services
from apps.canopy_sessions.models import Session
from apps.harness import initiator as who
from apps.harness.models import Runner, Turn
from apps.workspace_apps.models import AppTemplate, AppTemplateVersion
from apps.workspace_apps.services import _version_key
from apps.workspaces.links import scoped_url
from apps.workspaces.models import Workspace

from . import package as pkg
from . import trust
from .models import LocalAppInstallation, LocalAppRun, LocalAppSubmission

SWITCH = "local_apps"
#: A run with no heartbeat for this long is no longer shown as running.
RUN_STALE = dt.timedelta(minutes=6)
MAX_SEND_CHARS = 64_000
MAX_SESSIONS_PER_RUN = 5


class Refused(Exception):  # noqa: N818 — same name as workspace_apps.services.Refused
    """Refused, in words for the person. `status` is the HTTP answer."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def switched_on(workspace: Workspace) -> bool:
    return bool((workspace.settings or {}).get(SWITCH))


def set_switch(workspace: Workspace, on: bool) -> None:
    settings = dict(workspace.settings or {})
    settings[SWITCH] = bool(on)
    workspace.settings = settings
    workspace.save(update_fields=["settings", "updated_at"])


# --- the catalog ------------------------------------------------------------------------


def released(key: str):
    """This app's versions that may run, newest first."""
    rows = LocalAppSubmission.objects.filter(key=key, released_at__isnull=False, revoked_at__isnull=True)
    return sorted(rows, key=lambda s: _version_key(s.version), reverse=True)


def latest(key: str) -> LocalAppSubmission | None:
    rows = released(key)
    return rows[0] if rows else None


def catalog() -> list[LocalAppSubmission]:
    """The latest good version of each listed local app."""
    keys = AppTemplate.objects.filter(runtime=AppTemplate.LOCAL, listed=True, owner__isnull=True).values_list(
        "key", flat=True
    )
    return [s for s in (latest(k) for k in keys) if s is not None]


def package_bytes(sub: LocalAppSubmission) -> bytes:
    if sub.storage_key:
        return attachment_storage.get(sub.storage_key).body
    return bytes(sub.local or b"")


def _prior_release(sub: LocalAppSubmission) -> LocalAppSubmission | None:
    earlier = [
        s
        for s in LocalAppSubmission.objects.filter(key=sub.key, released_at__isnull=False).exclude(pk=sub.pk)
        if _version_key(s.version) < _version_key(sub.version)
    ]
    return max(earlier, key=lambda s: _version_key(s.version), default=None)


def review(sub: LocalAppSubmission) -> dict:
    """What a reviewer reads: the code's changes and what changed in the promise."""
    prior = _prior_release(sub)
    new = pkg.read(package_bytes(sub))
    old = pkg.read(package_bytes(prior)) if prior else None
    return {
        "prior_version": prior.version if prior else None,
        "trust_changes": trust.changes(prior.manifest if prior else None, sub.manifest),
        "diff": pkg.diff(old.files if old else None, new.files),
    }


# --- submit, release, revoke ------------------------------------------------------------


def _file_approval(sub: LocalAppSubmission, user, attempt: int) -> None:
    req, _ = approvals.create(
        kind="local_app.release",
        params={"submission": sub.pk, "sha256": sub.sha256},
        requested_by_user=user,
        reason=sub.note,
        idempotency_key=f"local-app-release-{sub.pk}-{attempt}",
    )
    sub.approval = req
    sub.save(update_fields=["approval"])


def submit(*, package_b64: str, note: str, workspace: Workspace, user) -> tuple[LocalAppSubmission, bool]:
    """Store a package and ask for its release. Returns (submission, created)."""
    try:
        data = base64.b64decode(package_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Refused("the package is not valid base64", 422) from exc
    try:
        package = pkg.read(data)
    except pkg.BadPackage as exc:
        raise Refused(str(exc), 422) from exc
    m = package.manifest
    if AppTemplate.objects.filter(key=m["key"], runtime=AppTemplate.WEB).exists():
        raise Refused(f"'{m['key']}' is already the name of an app that runs on Canopy", 409)
    existing = LocalAppSubmission.objects.filter(key=m["key"], version=m["version"]).first()
    if existing is not None:
        if existing.sha256 != package.sha256:
            raise Refused(
                f"{m['key']} {m['version']} was already submitted with different code. Versions never "
                "change: raise the version.",
                409,
            )
        if existing.approval and existing.approval.status == ApprovalRequest.EXPIRED:
            attempt = 1 + sum(
                1 for _ in ApprovalRequest.objects.filter(kind="local_app.release", params__submission=existing.pk)
            )
            _file_approval(existing, user, attempt)
        return existing, False
    top = latest(m["key"])
    if top is not None and _version_key(m["version"]) <= _version_key(top.version):
        raise Refused(f"version {m['version']} isn't newer than the released {top.version}", 409)
    storage_key = ""
    local = None
    if attachment_storage.is_configured():
        storage_key = f"local-apps/{m['key']}/{package.sha256}.zip"
        attachment_storage.put(storage_key, data, "application/zip")
    else:
        local = data
    with transaction.atomic():
        sub = LocalAppSubmission.objects.create(
            key=m["key"],
            version=m["version"],
            manifest=m,
            sha256=package.sha256,
            size=package.size,
            storage_key=storage_key,
            local=local,
            facts=pkg.facts(package),
            note=(note or "").strip()[:4000],
            workspace=workspace,
            submitted_by=user,
        )
        try:
            _file_approval(sub, user, 0)
        except approvals.ApprovalError as exc:
            raise Refused(str(exc), exc.status) from exc
    return sub, True


def release(sub: LocalAppSubmission) -> LocalAppSubmission:
    """Run by the approval's handler, and nothing else."""
    if sub.released_at or sub.revoked_at:
        raise Refused("this version was already decided")
    m = sub.manifest
    tmpl = AppTemplate.objects.filter(key=m["key"]).first()
    if tmpl is not None and tmpl.runtime != AppTemplate.LOCAL:
        raise Refused(f"'{m['key']}' is already the name of an app that runs on Canopy")
    if tmpl is None:
        tmpl = AppTemplate.objects.create(
            key=m["key"],
            name=m["name"],
            description=m["description"],
            owner=None,
            listed=True,
            runtime=AppTemplate.LOCAL,
        )
    else:
        tmpl.name, tmpl.description, tmpl.listed = m["name"], m["description"], True
        tmpl.save(update_fields=["name", "description", "listed"])
    version = AppTemplateVersion.objects.create(
        template=tmpl, version=sub.version, manifest=m, changelog=m["changelog"]
    )
    sub.release = version
    sub.released_at = timezone.now()
    sub.save(update_fields=["release", "released_at"])
    return sub


def revoke(sub: LocalAppSubmission, *, reason: str, user) -> LocalAppSubmission:
    if not sub.released_at:
        raise Refused("only a released version can be revoked")
    if not sub.revoked_at:
        sub.revoked_at = timezone.now()
        sub.revoked_reason = reason[:500]
        sub.revoked_by = user
        sub.save(update_fields=["revoked_at", "revoked_reason", "revoked_by"])
    return sub


# --- installing -------------------------------------------------------------------------


def computers_of(user):
    """Where this person may install: their own laptop runners. `private` isn't the
    test, because most laptops also serve fleet work."""
    return Runner.objects.filter(owner=user, kind=Runner.EMDASH).exclude(status=Runner.RETIRED).order_by("name")


def provider_of(stream: str) -> LocalAppSubmission | None:
    """The released app that provides `stream`, if exactly one does."""
    found = [s for s in catalog() if stream in s.manifest["provides"]]
    return found[0] if len(found) == 1 else None


def _clean_settings(manifest: dict, values: dict, workspace: Workspace, user, *, partial: bool = False) -> dict:
    spec = manifest["settings"]
    unknown = set(values) - set(spec)
    if unknown:
        raise Refused(f"unknown settings: {', '.join(sorted(unknown))}", 422)
    out = {}
    for name, field in spec.items():
        if name not in values:
            if field["required"] and not partial:
                raise Refused(f"{field['label']} is required", 422)
            continue
        value = values[name]
        if field["type"] == "bool":
            if not isinstance(value, bool):
                raise Refused(f"{field['label']} must be on or off", 422)
        elif not isinstance(value, str) or len(value) > 4000:
            raise Refused(f"{field['label']} must be text of at most 4000 characters", 422)
        elif field["type"] == "agent":
            _usable_agent(value, workspace, user)
        out[name] = value
    return out


def _usable_agent(slug: str, workspace: Workspace, user) -> Agent:
    agent = Agent.objects.filter(slug=slug, workspace=workspace).first()
    if agent is None or agent_access.decide(agent, user, verified=True).access == "none":
        raise Refused(f"you can't use the agent '{slug}' in this workspace", 403)
    return agent


def install(
    *, workspace: Workspace, key: str, user, runner_id, settings: dict, accept_digest: str
) -> LocalAppInstallation:
    """Install an app, and the source it listens to. `accept_digest` is the promise the
    person was shown, so a version released while they read can't slip past them."""
    if not switched_on(workspace):
        raise Refused("Apps that run on your computer are switched off in this workspace", 403)
    runner = computers_of(user).filter(pk=runner_id).first()
    if runner is None:
        raise Refused("that isn't one of your computers", 404)
    sub = latest(key)
    if sub is None:
        raise Refused(f"there is no released app '{key}'", 404)
    if trust.digest(sub.manifest) != accept_digest:
        raise Refused("this app changed while you were reading. Read it again.", 409)
    sources = []
    for stream in sub.manifest["consumes"]:
        source = provider_of(stream)
        if source is None:
            raise Refused(f"no single app provides '{stream}' yet", 409)
        sources.append(source)
    clean = _clean_settings(sub.manifest, settings or {}, workspace, user)
    with transaction.atomic():
        inst = _install_one(sub, user, runner, workspace, clean)
        for source in sources:
            _install_one(source, user, runner, workspace, {})
    return inst


def _install_one(sub, user, runner, workspace, settings) -> LocalAppInstallation:
    template = sub.release.template
    inst, created = LocalAppInstallation.objects.get_or_create(
        user=user,
        runner=runner,
        template=template,
        defaults={
            "workspace": workspace,
            "settings": settings,
            "accepted": sub,
            "accepted_digest": trust.digest(sub.manifest),
        },
    )
    if not created:
        inst.workspace, inst.accepted, inst.accepted_digest = workspace, sub, trust.digest(sub.manifest)
        inst.settings = {**inst.settings, **settings}
        inst.save()
    return inst


def configure(inst: LocalAppInstallation, values: dict, user) -> LocalAppInstallation:
    manifest = (latest(inst.template.key) or inst.accepted).manifest
    inst.settings = {**inst.settings, **_clean_settings(manifest, values, inst.workspace, user, partial=True)}
    inst.save(update_fields=["settings", "updated_at"])
    return inst


def accept(inst: LocalAppInstallation, digest: str) -> LocalAppInstallation:
    sub = latest(inst.template.key)
    if sub is None or trust.digest(sub.manifest) != digest:
        raise Refused("this app changed while you were reading. Read it again.", 409)
    inst.accepted, inst.accepted_digest = sub, digest
    inst.save(update_fields=["accepted", "accepted_digest", "updated_at"])
    return inst


def resolve(inst: LocalAppInstallation) -> dict:
    """Which version this installation runs at its next Start, and why it might not.

    The newest good version, if its trust surface is what they accepted. If that
    changed, they stay on what they accepted until they accept the new one, unless
    what they accepted has been revoked."""
    top = latest(inst.template.key)
    changes: list[str] = []
    needs_acceptance = False
    version = top
    refusal = None
    if top is None:
        version = None
        refusal = "This app has been withdrawn."
    elif trust.digest(top.manifest) != inst.accepted_digest:
        needs_acceptance = True
        changes = trust.changes(inst.accepted.manifest, top.manifest)
        if inst.accepted.revoked_at:
            version = None
            refusal = f"The version you have was withdrawn: {inst.accepted.revoked_reason}. Review the new one first."
        else:
            version = inst.accepted
    if refusal is None and not switched_on(inst.workspace):
        refusal = "Apps that run on your computer are switched off in this workspace."
    if refusal is None and version is not None:
        settings_spec = version.manifest["settings"]
        for name, field in settings_spec.items():
            if field["required"] and name not in inst.settings:
                refusal = f"Set '{field['label']}' first."
                break
    if refusal is None and version is not None and (slug := agent_slug(inst, version)):
        try:
            _usable_agent(slug, inst.workspace, inst.user)
        except Refused as exc:
            refusal = str(exc)
    return {"version": version, "needs_acceptance": needs_acceptance, "changes": changes, "refusal": refusal}


def agent_slug(inst: LocalAppInstallation, version: LocalAppSubmission) -> str:
    for name, field in version.manifest["settings"].items():
        if field["type"] == "agent":
            return inst.settings.get(name) or ""
    return ""


def source_key_of(version: LocalAppSubmission, inst: LocalAppInstallation) -> str | None:
    for stream in version.manifest["consumes"]:
        for other in LocalAppInstallation.objects.filter(user=inst.user, runner=inst.runner).select_related("template"):
            sub = latest(other.template.key)
            if sub is not None and stream in sub.manifest["provides"]:
                return other.template.key
    return None


def live_run(inst: LocalAppInstallation) -> LocalAppRun | None:
    return inst.runs.filter(finished_at__isnull=True, last_beat_at__gte=timezone.now() - RUN_STALE).first()


# --- runs -------------------------------------------------------------------------------


def start_run(inst: LocalAppInstallation, version: str) -> LocalAppRun:
    state = resolve(inst)
    if state["refusal"]:
        raise Refused(state["refusal"], 403)
    if state["version"].version != version:
        raise Refused(f"run version {state['version'].version}, not {version}", 409)
    # A run that never reported its end (a Mac that slept and never woke it) ends here.
    inst.runs.filter(finished_at__isnull=True).update(finished_at=timezone.now(), stop_reason="replaced")
    return LocalAppRun.objects.create(installation=inst, version=state["version"])


def stop_wanted(run: LocalAppRun) -> str:
    """Why this run must stop, or "" if it may go on. Read at each heartbeat and each
    Canopy call, which is what makes revoking and the workspace switch reach running copies."""
    if run.finished_at:
        return "finished"
    if run.version.revoked_at:
        return "revoked"
    if run.stop_requested:
        return "stopped_from_website"
    if not switched_on(run.installation.workspace):
        return "switched_off"
    return ""


def heartbeat(run: LocalAppRun, *, state: str, counts: dict) -> dict:
    LocalAppRun.objects.filter(pk=run.pk).update(last_beat_at=timezone.now(), state=state[:40], counts=_counts(counts))
    run.refresh_from_db()
    reason = stop_wanted(run)
    return {"stop": bool(reason), "reason": reason}


def _counts(counts: dict) -> dict:
    # Numbers only: a heartbeat is no way for anything said to reach the server's rows.
    return {
        str(k)[:40]: v
        for k, v in list((counts or {}).items())[:20]
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def finish(run: LocalAppRun, *, reason: str, counts: dict) -> None:
    if run.finished_at:
        return
    run.finished_at = timezone.now()
    run.stop_reason = reason[:80]
    run.counts = _counts(counts)
    run.save(update_fields=["finished_at", "stop_reason", "counts"])


def canopy_call(run: LocalAppRun, op: str, args: dict) -> dict:
    """One Canopy call a run asked for. Refusals come back as `{status, body}` so an
    app sees them as data; only something the host got wrong is an HTTP error."""
    reason = stop_wanted(run)
    if reason:
        return {"status": 410, "body": {"detail": f"this run must stop: {reason}"}}
    if op not in run.version.manifest["canopy"]:
        return {"status": 403, "body": {"detail": f"this app's manifest doesn't declare {op}"}}
    try:
        return {"status": 200, "body": _OPS[op](run, args if isinstance(args, dict) else {})}
    except Refused as exc:
        return {"status": exc.status, "body": {"detail": str(exc)}}


def _session_of(run: LocalAppRun, raw) -> Session:
    try:
        session_id = str(uuid.UUID(str(raw)))
    except ValueError as exc:
        raise Refused("not a session id", 422) from exc
    if session_id not in run.session_ids:
        raise Refused("this run didn't open that session", 403)
    session = Session.objects.filter(pk=session_id).first()
    if session is None:
        raise Refused("that session is gone", 404)
    return session


def _create_session(run: LocalAppRun, args: dict) -> dict:
    inst = run.installation
    if len(run.session_ids) >= MAX_SESSIONS_PER_RUN:
        raise Refused("this run has opened as many sessions as it may", 409)
    agent = _usable_agent(agent_slug(inst, run.version), inst.workspace, inst.user)
    session = session_services.create_session(
        workspace=inst.workspace,
        created_by=inst.user,
        agent=agent,
        title=str(args.get("title") or run.version.manifest["name"])[:200],
    )
    run.session_ids = [*run.session_ids, str(session.pk)]
    run.save(update_fields=["session_ids"])
    return {"id": str(session.pk), "url": scoped_url(inst.workspace, f"/sessions/{session.pk}")}


def _send(run: LocalAppRun, args: dict) -> dict:
    session = _session_of(run, args.get("session_id"))
    text = args.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_SEND_CHARS:
        raise Refused(f"text must be non-empty and at most {MAX_SEND_CHARS} characters", 422)
    client_id = str(args.get("client_id") or "")[:120]
    inst = run.installation
    try:
        _message, turn = doors.send_from(
            "rest",
            session=session,
            text=text,
            user=inst.user,
            client_id=client_id,
            runner_requirements=(),
            chat_surface=False,
            origin="api",
            initiator=who.for_user(inst.user, via=f"local-app:{inst.template.key}", assurance=who.RUNNER_KEY),
        )
    except ValueError as exc:
        raise Refused(str(exc), 422) from exc
    return {"turn_id": str(turn.pk) if turn else None}


def _turn_status(run: LocalAppRun, args: dict) -> dict:
    try:
        turn = Turn.objects.filter(pk=uuid.UUID(str(args.get("turn_id")))).first()
    except ValueError as exc:
        raise Refused("not a turn id", 422) from exc
    if turn is None or str(turn.chat_session_id) not in run.session_ids:
        raise Refused("this run didn't start that turn", 403)
    return {"status": turn.status}


_OPS = {"sessions.create": _create_session, "sessions.send": _send, "turns.status": _turn_status}
