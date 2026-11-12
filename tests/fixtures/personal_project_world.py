"""The personal-project guard's world and its GET walk, shared by the suites that walk every route
as someone who must not read it (`tests/test_personal_project_never_leaks.py`,
`tests/test_guest_members_never_leak.py`). A sentinel naming its field is written into every text
field the world holds, so a failure says which one leaked."""
from __future__ import annotations

import json
import uuid

from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agent_runs.models import AgentRun
from apps.agents.models import (
    Agent,
    AgentProject,
    AgentTask,
    AgentTaskAction,
    AgentTaskHumanWhy,
    EngagementEvent,
    KbSource,
    KbVersion,
    ProjectBacking,
    ProjectMilestone,
    ProjectParticipant,
    ProjectService,
    ProjectShare,
    ProjectUpdate,
    SavedView,
    TaskEvent,
    TaskRelation,
    Vault,
    VaultAttachment,
)
from apps.api.api import api
from apps.contacts.models import PersonFact
from apps.contacts.services import person_for
from apps.dynamic_views.models import DynamicView
from apps.feedback.models import FeedbackRound
from apps.harness.models import Turn
from apps.huddles.models import CheckinRun, ProjectCheckin
from apps.meetings.models import Meeting, MeetingArtifact
from apps.reviews.models import ReviewRequest
from apps.storyboards.models import Storyboard
from apps.walkthroughs.models import Walkthrough
from apps.workspaces.models import Workspace, WorkspaceMembership

MARK = "sntlxq"
PUBLIC_MARK = "publicctrl"


def S(field: str) -> str:
    """A sentinel naming the field it was written to, so a failure says which one leaked."""
    return f"{MARK}-{field}"


def _make_world() -> dict:
    lead = User.objects.create_user("lead", "lead@dimagi.com", "pw")
    editor = User.objects.create_user("editor", "editor@dimagi.com", "pw")
    wsadmin = User.objects.create_user("wsadmin", "wsadmin@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="guardws", display_name="Guard", created_by=lead)
    WorkspaceMembership.objects.create(user=lead, workspace=ws, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(user=editor, workspace=ws, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(user=wsadmin, workspace=ws, role=WorkspaceMembership.ADMIN)

    lead_login = User.objects.create_user("leadagent", "leadagent@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=lead_login, workspace=ws, role=WorkspaceMembership.EDITOR)
    # The admin OWNS the lead agent: an agent's admins are the widest way into its work.
    lead_agent = Agent.objects.create(slug="guard-lead", name="Guard Lead", workspace=ws, owner=wsadmin, user=lead_login)
    outsider_agent = Agent.objects.create(slug="guard-outsider", name="Guard Outsider", workspace=ws, owner=editor)

    project = AgentProject.objects.create(
        workspace=ws, handle=f"{MARK}-handle", name=S("project.name"), outcome=S("project.outcome"),
        notes=S("project.notes"), owner_note=S("project.owner_note"), owner_user=lead, agent=lead_agent,
        ext_id=f"{MARK}-P1", created_by_agent=lead_agent, repo_slug=f"{MARK}-repo",
        visibility=AgentProject.PERSONAL, source_url=f"https://example.test/huddles/{MARK}-huddle",
        drive_folder_url=f"https://example.test/{S('project.drive')}",
        links=[{"label": S("project.link"), "url": "https://example.test/x"}])
    project.member_agents.add(lead_agent)
    twin = AgentProject.objects.create(workspace=ws, handle=f"{PUBLIC_MARK}-handle", name=f"{PUBLIC_MARK}-project",
                                       owner_user=lead, agent=lead_agent, ext_id="P77", created_by_agent=lead_agent)
    public_task = AgentTask.objects.create(workspace=ws, title=f"{PUBLIC_MARK}-task", agent=lead_agent,
                                           created_by_agent=lead_agent, ext_id="T900", project=twin,
                                           status=AgentTask.TODO)

    def task(title: str, **kw):
        return AgentTask.objects.create(
            workspace=ws, project=project, title=S(title), next_action=S(f"{title}.next"),
            rationale=S(f"{title}.rationale"), plan=S(f"{title}.plan"), notes=S(f"{title}.notes"),
            source_url=f"https://example.test/huddles/{MARK}-huddle", created_by_agent=lead_agent,
            ask_body=S(f"{title}.ask"), **kw)

    # Every way a task is "mine" for someone who may not read it: the guard's whole point.
    held_by_editor = task("task.editor", status=AgentTask.SUGGESTED, assignee_user=editor, waiting_on_user=editor,
                          ask_kind=AgentTask.ASK_REVIEW, ext_id=f"{MARK}-T1", kind=AgentTask.IDEA)
    on_outsider = task("task.outsider", status=AgentTask.TODO, agent=outsider_agent, ext_id=f"{MARK}-T2",
                       waiting_on_user=wsadmin, ask_kind=AgentTask.ASK_QUESTION)
    on_lead = task("task.lead", status=AgentTask.IN_PROGRESS, agent=lead_agent, ext_id=f"{MARK}-T3")
    subtask = task("task.sub", status=AgentTask.TODO, agent=lead_agent, parent=on_lead, ext_id=f"{MARK}-T4")
    # A recurring template and its instance: the recurring/template filters and spawn must keep them in.
    template = task("task.template", status=AgentTask.BACKLOG, agent=lead_agent, ext_id=f"{MARK}-T5",
                    recurrence={"every": "week", "auto": True})
    instance = task("task.instance", status=AgentTask.DONE, agent=lead_agent, ext_id=f"{MARK}-T6", template=template,
                    template_period=timezone.localdate())
    TaskEvent.objects.create(task=instance, kind=TaskEvent.FIELD, field="status", old="todo", new="done")
    comment = AgentTaskAction.objects.create(task=on_lead, action="comment", comment=S("comment.body"), by="lead@dimagi.com",
                                             agent=lead_agent, status="applied")
    TaskEvent.objects.create(task=on_lead, kind=TaskEvent.COMMENT, action=comment)
    # A task of a project anyone reads pointing into the personal one, and a subtask of it.
    outside_sub = AgentTask.objects.create(workspace=ws, title=f"{PUBLIC_MARK}-sub", parent=on_lead, ext_id="T901",
                                           created_by_agent=lead_agent, agent=lead_agent)
    TaskRelation.objects.create(from_task=public_task, to_task=on_lead, kind="related")
    TaskRelation.objects.create(from_task=on_lead, to_task=outside_sub, kind="blocks")
    twin.spawned_from = on_lead
    twin.save(update_fields=["spawned_from"])
    AgentTaskHumanWhy.objects.create(task=held_by_editor, quote=S("why.quote"), who_name=S("why.who"),
                                     source_url=f"https://example.test/{S('why.url')}")
    action = AgentTaskAction.objects.create(task=on_lead, action="reply", comment=S("action.comment"), by="lead@dimagi.com",
                                            agent=lead_agent, status="pending")
    AgentTaskAction.objects.create(task=held_by_editor, action="reply", comment=S("action2.comment"), by="x",
                                   agent=outsider_agent, status="pending")
    TaskEvent.objects.create(task=on_lead, kind=TaskEvent.ACTION, action=action, new=S("event.new"))

    ProjectMilestone.objects.create(project=project, name=S("milestone.name"), description=S("milestone.description"))
    ProjectUpdate.objects.create(project=project, health="on_track", body=S("update.body"), status=ProjectUpdate.PUBLISHED,
                                 published_at=timezone.now())
    ProjectUpdate.objects.create(project=project, health="on_track", body=S("draft.body"), status=ProjectUpdate.DRAFT)
    kb = ProjectService.objects.create(project=project, kind="kb", name=f"{MARK}-kb", kb_key=f"{MARK}/kb.sqlite.gz",
                                       kb_sha256="0" * 64)
    ProjectService.objects.create(project=project, kind="skill", name=f"{MARK}-skill", description=S("skill.description"),
                                  instructions=S("skill.instructions"))
    ProjectService.objects.create(project=project, kind="mcp", name=f"{MARK}-mcp", description=S("mcp.description"),
                                  url=f"https://example.test/{S('mcp.url')}")
    ProjectService.objects.create(project=project, kind="plugin", name=f"{MARK}-plugin",
                                  url=f"https://example.test/{S('plugin.url')}")
    KbSource.objects.create(project=project, kind="web", name=S("kbsource.name"), config={"n": S("kbsource.config")})
    version = KbVersion.objects.create(project=project, number=1, kind="full", summary={"note": S("kbversion.summary")}, status="ready")
    kb.kb_version = version
    kb.save(update_fields=["kb_version"])

    # The auto-filled people: taking part in a project never opens it.
    editor_person, admin_person = person_for(user=editor), person_for(user=wsadmin)
    for person in (editor_person, admin_person):
        ProjectParticipant.objects.create(project=project, person=person, source="manual",
                                          summary={"goal": S("participant.summary")})
    ProjectBacking.objects.create(project=project, person=editor_person, stance="backs", line=S("backing.line"))
    PersonFact.objects.create(person=editor_person, workspace=ws, project=project, kind="project", statement=f"{PUBLIC_MARK} fact")
    PersonFact.objects.create(person=admin_person, workspace=ws, project=project, kind="project", statement=f"{PUBLIC_MARK} fact")
    EngagementEvent.objects.create(workspace=ws, kind="proposed", project=project, user=editor, task=held_by_editor)
    EngagementEvent.objects.create(workspace=ws, kind="proposed", task=on_outsider, meta={"level": "task"})

    # What the services refuse on a personal project, there anyway: reads must not rely on that.
    other = Workspace.objects.create(slug="guardws-sub", display_name="Sub", created_by=lead, parent=ws)
    WorkspaceMembership.objects.create(user=editor, workspace=other, role=WorkspaceMembership.EDITOR)
    ProjectShare.objects.create(project=project, workspace=other, added_by=lead)
    checkin = ProjectCheckin.objects.create(project=project, name=f"{MARK}-checkin", topic=S("checkin.topic"))
    run = CheckinRun.objects.create(project=project, checkin=checkin, checkin_name=f"{MARK}-checkin",
                                    slot=timezone.now(), material=S("run.material"), summary=S("run.summary"),
                                    huddle_id=f"{MARK}-huddle")
    view = DynamicView.objects.create(workspace=ws, project=project, slug=f"{MARK}-view", title=S("view.title"),
                                      description=S("view.description"), code=f"// {S('view.code')}")
    saved = SavedView.objects.create(workspace=ws, owner=lead, name=f"{PUBLIC_MARK}-saved", shared=True)
    vault = Vault.objects.create(workspace=ws, kind=Vault.WORKSPACE, name=f"{PUBLIC_MARK}-vault", created_by=wsadmin)
    VaultAttachment.objects.create(vault=vault, project=project)
    doc = AgentRun.objects.create(agent=lead_agent, project=project, ext_id="guard-doc", subject="guard", label="doc", kind="doc")
    storyboard = Storyboard.objects.create(slug=f"{PUBLIC_MARK}-board", title="board", workspace=ws, agent_project=project)
    walkthrough = Walkthrough.objects.create(title="wt", kind="html", owner=editor, drive_file_id="f", drive_folder_id="d",
                                             content_type="text/html", size_bytes=1, agent_project=project, workspace=ws,
                                             run_id=str(doc.pk))
    review = ReviewRequest.objects.create(run_id=str(doc.pk), gate="narrative", request_json={}, agent_project=project,
                                          workspace=ws)
    FeedbackRound.objects.create(target_kind="storyboard", target_ref=storyboard.slug, workspace=ws, number=1, task=on_lead)

    # The board turn: a prompt carrying the project, with a report.
    turn = Turn.objects.create(agent=lead_agent, origin=Turn.ORIGIN_API, prompt=S("turn.prompt"),
                               raised_from_task=on_lead, task_ext_ids=[f"{MARK}-T3"],
                               origin_ref={"task_title": S("turn.origin")}, result_note=S("turn.result"),
                               report_title=S("turn.report_title"), report_summary=S("turn.report_summary"),
                               session_slug=f"{MARK}-slug", work_product_urls=[f"https://example.test/{S('turn.url')}"],
                               reported_at=timezone.now(), initiator_user=wsadmin, enqueued_by=wsadmin)

    meeting = Meeting.objects.create(workspace=ws, owner=lead, project=project, agent=lead_agent,
                                     title=S("meeting.title"), attendees=[{"name": S("meeting.attendee"), "email": ""}])
    MeetingArtifact.objects.create(meeting=meeting, kind="transcript", text=S("meeting.text"), sha256="x",
                                   source=f"{MARK}-src"[:40], source_url=f"https://example.test/{S('meeting.url')}")
    project.meeting_instructions = S("project.meeting_instructions")
    project.save(update_fields=["meeting_instructions"])

    return dict(meeting=meeting, ws=ws, lead=lead, editor=editor, wsadmin=wsadmin, lead_login=lead_login, lead_agent=lead_agent,
                outsider_agent=outsider_agent, project=project, twin=twin, public_task=public_task,
                tasks=[held_by_editor, on_outsider, on_lead, subtask, template, instance], checkin=checkin, run=run, view=view, saved=saved,
                turn=turn, doc=doc, vault=vault, storyboard=storyboard, walkthrough=walkthrough, review=review,
                editor_person=editor_person, admin_person=admin_person, version=version, action=action)


# ---- what to call each route with -------------------------------------------------

#: Parameters that name something a project hangs off: the values that reach it.
def _project_values(w: dict) -> dict:
    project, tasks = w["project"], w["tasks"]
    return {
        "slug": [w["lead_agent"].slug, w["outsider_agent"].slug],
        "workspace": [w["ws"].slug],
        "ws": [w["ws"].slug],
        "handle": [project.handle],
        "ref": [project.handle, project.ext_id, tasks[2].key, tasks[2].ext_id],
        "key": [t.key for t in tasks],
        "task": [tasks[2].key, tasks[0].ext_id],
        "ext_id": [w["doc"].ext_id],
        "name": [w["checkin"].name],
        "meeting_id": [str(w["meeting"].pk)],
        "turn_id": [str(w["turn"].pk)],
        "run_id": [str(w["run"].pk), str(w["doc"].pk)],
        "huddle_id": [f"{MARK}-huddle"],
        "person_id": [str(w["editor_person"].pk), str(w["admin_person"].pk)],
        "person": [str(w["editor_person"].pk)],
        "view_id": [str(w["view"].pk)],
        "vault_id": [str(w["vault"].pk)],
        "wid": [str(w["walkthrough"].pk)],
        "rid": [str(w["review"].pk)],
        "number": ["1"],
    }


#: Parameters that name nothing about a project (a runner, a token, a mailbox …). The
#: value finds nothing or something unrelated, and the route still has to answer.
NOT_ABOUT_A_PROJECT = {
    **{name: [str(uuid.UUID(int=0))] for name in (
        "runner_id", "session_id", "request_id", "connection_id", "lease_id", "sign_off_id", "attachment_id",
        "ingest_id", "mint_id", "domain_id", "uuid", "grant_id", "source_id", "record_id", "file_id",
        "submission_id", "exec_id", "task_id", "app_id", "invitation_id", "fleet_id", "handoff_id", "investigation_id",
        "thread_id", "approval_id", "contact_id", "entry_id")},
    "token": ["guard-token"],
    "provider": ["salesforce"],
    "tool_call_id": ["guard-call"],
    "collection": ["guard-collection"],
    "repo_slug": ["guard-repo"],
    "narrative_slug": ["guard-narrative"],
    "user_id": ["1"],
    "kind": ["project"],
    "user_code": ["GUARD-CODE"],
    "id": ["1"],
}


def path_values(w: dict) -> dict:
    return {**NOT_ABOUT_A_PROJECT, **_project_values(w)}


def _query_variants(w: dict) -> dict[str, list[str]]:
    """The filters that widen or aim a list, each tried on its own."""
    project = w["project"]
    return {"project": [project.address, project.handle, project.ext_id], "agent": [w["lead_agent"].slug],
            "status": ["", "open", "suggested"], "waiting": ["me"], "assignee": ["me"], "created_by": ["agent:guard-lead"],
            "parent": [w["tasks"][2].key], "ask": ["open"], "kind": ["idea"], "category": ["triage"], "q": [MARK],
            "agent_project": [str(project.pk), project.ext_id], "scope": ["all"], "days": ["30"], "repo_slug": [project.repo_slug],
            "workspace": [w["ws"].slug], "ws": [w["ws"].slug], "huddle": [f"{MARK}-huddle"], "checkin": [w["checkin"].name],
            "mine": ["false"], "subsystem": ["agents"], "recurring": ["true", "false"],
            "template": [w["tasks"][4].key, w["tasks"][4].key.lower()]}


_REQUIRED_QUERY = {"email": "editor@dimagi.com", "agent": "guard-lead", "workspace": "guardws", "start": "2026-01-05",
                   "agents": "guard-lead", "before": "2030-01-01T00:00:00Z", "platform": "darwin", "runner_id": str(uuid.UUID(int=0))}


def _schema() -> dict:
    return json.loads(json.dumps(api.get_openapi_schema(), default=str))


def _get_routes():
    for path, item in _schema()["paths"].items():
        if "get" in item:
            yield path, item["get"]


def _body(resp) -> str:
    if getattr(resp, "streaming", False):
        return b"".join(resp.streaming_content).decode("utf-8", "replace")
    return resp.content.decode("utf-8", "replace")


def _client(user) -> Client:
    c = Client(raise_request_exception=False)
    c.force_login(user)
    return c


