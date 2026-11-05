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

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.agents import turn_context
from apps.agents.models import Agent
from apps.approvals import services as approvals
from apps.approvals.models import ApprovalRequest
from apps.canopy_sessions import access as session_access
from apps.canopy_sessions import attachment_storage, send_paths
from apps.canopy_sessions import services as session_services
from apps.canopy_sessions.models import Session
from apps.harness import initiator as who
from apps.harness.models import Runner, Turn
from apps.meetings import services as meetings
from apps.workspace_apps.models import AppTemplate, AppTemplateVersion
from apps.workspace_apps.services import _version_key
from apps.workspaces import permissions as perms
from apps.workspaces.links import scoped_url
from apps.workspaces.models import Workspace

from . import package as pkg
from . import trust
from .models import LocalAppInstallation, LocalAppRun, LocalAppSubmission

#: A run with no heartbeat for this long is no longer shown as running.
RUN_STALE = dt.timedelta(minutes=6)
MAX_SEND_CHARS = 64_000
MAX_SESSIONS_PER_RUN = 5


class Refused(Exception):  # noqa: N818 — same name as workspace_apps.services.Refused
    """Refused, in words for the person. `status` is the HTTP answer."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


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
    # Publishing has no reviewer for now, and a new version reaches people at their next Start.
    # So an app's name stays with the workspace that first released it: anyone can publish a
    # new app, and only its own people can change one others already run.
    first = LocalAppSubmission.objects.filter(key=m["key"], released_at__isnull=False).order_by("released_at").first()
    if first is not None and first.workspace_id != workspace.pk:
        raise Refused(f"'{m['key']}' is published from another workspace. Choose another key.", 403)
    top = latest(m["key"])
    if top is not None and _version_key(m["version"]) <= _version_key(top.version):
        raise Refused(f"version {m['version']} isn't newer than the released {top.version}", 409)
    # Review is the protection, so what can't all be read on the review page can't be submitted.
    before = pkg.read(package_bytes(top)).files if top else None
    if pkg.diff_lines(before, package.files) > pkg.MAX_DIFF_LINES:
        raise Refused(f"this changes over {pkg.MAX_DIFF_LINES} lines, more than a reviewer can read. Split it up.", 422)
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
        if settings.LOCAL_APPS_REQUIRE_APPROVAL:
            try:
                _file_approval(sub, user, 0)
            except approvals.ApprovalError as exc:
                raise Refused(str(exc), exc.status) from exc
        else:
            release(sub)
    return sub, True


def release(sub: LocalAppSubmission) -> LocalAppSubmission:
    """Run by the approval's handler, or by `submit` while approval is off. Nothing else."""
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


def _clean_settings(manifest: dict, values: dict, user, *, partial: bool = False) -> dict:
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
            _usable_agent(value, user)
        out[name] = value
    return out


def _usable_agent(slug: str, user) -> Agent:
    """An agent this person may use, in any workspace they're in."""
    agent = Agent.objects.select_related("workspace").filter(slug=slug).first()
    # verified=False because that's how a run's turn is judged (its assurance is the runner's
    # key), so this answers the same as the turn will rather than promising more.
    if (
        agent is None
        or not perms.can(user, agent.workspace, perms.READ)
        or turn_context.for_asker(agent, user, verified=False).refused
    ):
        raise Refused(f"you can't use the agent '{slug}'", 403)
    return agent


def _workspace_for(manifest: dict, settings: dict, user):
    """Sessions land where the chosen agent lives. A user app has no workspace of its own."""
    for name, field in manifest["settings"].items():
        if field["type"] == "agent" and settings.get(name):
            return _usable_agent(settings[name], user).workspace
    return None


def install(*, key: str, user, runner_id, settings: dict, accept_digest: str) -> LocalAppInstallation:
    """Install an app for this person. `accept_digest` is the
    promise they were shown, so a version released while they read can't slip past them."""
    runner = computers_of(user).filter(pk=runner_id).first()
    if runner is None:
        raise Refused("that isn't one of your computers", 404)
    sub = latest(key)
    if sub is None:
        raise Refused(f"there is no released app '{key}'", 404)
    if trust.digest(sub.manifest) != accept_digest:
        raise Refused("this app changed while you were reading. Read it again.", 409)
    clean = _clean_settings(sub.manifest, settings or {}, user)
    workspace = _workspace_for(sub.manifest, clean, user)
    inst, created = LocalAppInstallation.objects.get_or_create(
        user=user,
        runner=runner,
        template=sub.release.template,
        defaults={
            "workspace": workspace,
            "settings": clean,
            "accepted": sub,
            "accepted_digest": trust.digest(sub.manifest),
        },
    )
    if not created:
        inst.workspace, inst.accepted, inst.accepted_digest = workspace, sub, trust.digest(sub.manifest)
        inst.settings = {**inst.settings, **clean}
        inst.save()
    return inst


def configure(inst: LocalAppInstallation, values: dict, user) -> LocalAppInstallation:
    manifest = (latest(inst.template.key) or inst.accepted).manifest
    inst.settings = {**inst.settings, **_clean_settings(manifest, values, user, partial=True)}
    inst.workspace = _workspace_for(manifest, inst.settings, user)
    inst.save(update_fields=["settings", "workspace", "updated_at"])
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
    if refusal is None and version is not None:
        settings_spec = version.manifest["settings"]
        for name, field in settings_spec.items():
            if field["required"] and name not in inst.settings:
                refusal = f"Set '{field['label']}' first."
                break
    if refusal is None and version is not None and (slug := agent_slug(inst, version)):
        try:
            _usable_agent(slug, inst.user)
        except Refused as exc:
            refusal = str(exc)
    return {"version": version, "needs_acceptance": needs_acceptance, "changes": changes, "refusal": refusal}


def agent_slug(inst: LocalAppInstallation, version: LocalAppSubmission) -> str:
    for name, field in version.manifest["settings"].items():
        if field["type"] == "agent":
            return inst.settings.get(name) or ""
    return ""


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
    Canopy call, so a decision made after Start reaches running copies: revoking, the
    and the person losing their access."""
    if run.finished_at:
        return "finished"
    if run.version.revoked_at:
        return "revoked"
    if run.stop_requested:
        return "stopped_from_website"
    inst = run.installation
    if slug := agent_slug(inst, run.version):
        try:
            _usable_agent(slug, inst.user)
        except Refused:
            return "no_longer_allowed_to_use_agent"
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
    # Having opened it isn't standing: every route into a chat asks the one ACL, so
    # someone removed from this session stops being able to send here too.
    if not session_access.can_write(run.installation.user, session):
        raise Refused("you can no longer write in that session", 403)
    return session


def _create_session(run: LocalAppRun, args: dict) -> dict:
    inst = run.installation
    if len(run.session_ids) >= MAX_SESSIONS_PER_RUN:
        raise Refused("this run has opened as many sessions as it may", 409)
    agent = _usable_agent(agent_slug(inst, run.version), inst.user)
    session = session_services.create_session(
        workspace=agent.workspace,
        created_by=inst.user,
        agent=agent,
        title=str(args.get("title") or run.version.manifest["name"])[:200],
    )
    run.session_ids = [*run.session_ids, str(session.pk)]
    run.save(update_fields=["session_ids"])
    return {"id": str(session.pk), "url": scoped_url(agent.workspace, f"/sessions/{session.pk}")}


def _send(run: LocalAppRun, args: dict) -> dict:
    session = _session_of(run, args.get("session_id"))
    text = args.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_SEND_CHARS:
        raise Refused(f"text must be non-empty and at most {MAX_SEND_CHARS} characters", 422)
    client_id = str(args.get("client_id") or "")[:120]
    inst = run.installation
    try:
        _message, turn = send_paths.send_from(
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
    # A refused turn is written as cancelled with the text kept on it. Say so, or the app
    # would count it as sent and go on sending a conversation nobody is reading.
    if turn is not None and turn.status == Turn.CANCELLED:
        raise Refused("your agent didn't accept that", 403)
    return {"turn_id": str(turn.pk) if turn else None}


def _turn_status(run: LocalAppRun, args: dict) -> dict:
    try:
        turn = Turn.objects.filter(pk=uuid.UUID(str(args.get("turn_id")))).first()
    except ValueError as exc:
        raise Refused("not a turn id", 422) from exc
    if turn is None or str(turn.chat_session_id) not in run.session_ids:
        raise Refused("this run didn't start that turn", 403)
    return {"status": turn.status}


def _save_meeting(run: LocalAppRun, args: dict) -> dict:
    """The transcript of a meeting that ended, private to the person until they attach it."""
    inst = run.installation
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Refused("text must be non-empty", 422)
    agent = _usable_agent(agent_slug(inst, run.version), inst.user)
    actor = meetings.Actor(user=inst.user, login=inst.user)
    try:
        meeting, _created = meetings.create(
            actor,
            title=str(args.get("title") or ""),
            agent=agent,
            artifact={"kind": "transcript", "text": text, "source": str(args.get("source") or inst.template.key)},
        )
    except meetings.Refused as exc:
        raise Refused(str(exc), exc.status) from exc
    return {"id": str(meeting.pk), "url": scoped_url(meeting.workspace_id, f"/meetings/{meeting.pk}")}


_OPS = {
    "sessions.create": _create_session,
    "sessions.send": _send,
    "turns.status": _turn_status,
    "meetings.save": _save_meeting,
}
