"""E2E seed: a user + an authenticated session + the echo agent with a spread of
tasks and one pending command. Writes the session key to .auth/session.txt for
Playwright to use as a cookie. Run via `manage.py shell -c` from the repo root."""
import datetime as dt
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore

from apps.agents.models import (
    Agent, AgentSkill, AgentSync, AgentTask, AgentTaskCommand, AgentWorkProduct,
)
from apps.harness.models import Item
from apps.reviews.models import ReviewRequest
from apps.workspaces import services as wsvc

FLEET_AUDIT_RUN_ID = "ada-fleet-audit-2026-07-14"
FLEET_AUDIT_BATCH = "fleet-audit-2026-07-14"

User = get_user_model()
user, _ = User.objects.get_or_create(username="e2e", defaults={"email": "e2e@dimagi.com"})

# Agents are workspace-scoped: give the e2e user a workspace + membership and
# assign Echo to it (mirrors production, where register() assigns a workspace),
# so /w/<slug>/agents resolves and the frontend switcher/redirects work.
ws = wsvc.ensure_default_workspace()  # slug "dimagi"; owner = first user (the e2e user)
if ws is not None:
    wsvc.ensure_member(ws, user)

a, _ = Agent.objects.update_or_create(slug="echo", defaults=dict(
    name="Echo", email="echo@dimagi-ai.com", description="Marketing agent for Connect.",
    persona="Email-driven marketing agent.", workspace=ws))
a.tasks.all().delete()
a.commands.all().delete()


def t(**k):
    return AgentTask.objects.create(agent=a, **k)


t(ext_id="t1", title="ZEGCAWIS polio AFP story", next_action="Get consent to interview the FLW",
  status="suggested", owner="Matt", assigned="Matt", confidence="high",
  rationale="Strong near-miss; timely AFP detection fed national surveillance.",
  source_url="https://example.com/zegcawis", position=0)
t(ext_id="t2", title="Brand-voice loop", next_action="Propose a draft to edits design",
  status="suggested", owner="Amie", assigned="Echo", confidence="low",
  rationale="Learn the house voice from edits.", position=1)
t(ext_id="t3", title="PRIDE cholera story", next_action="Run the interview",
  status="in_progress", owner="Matt", assigned="Sarvesh", position=2)
t(ext_id="t4", title="Ideas backlog upkeep", next_action="Append new ideas",
  status="in_progress", owner="Matt", assigned="Echo", position=3)
t(ext_id="t5", title="Agent workspace shipped", next_action="", status="done",
  owner="Jonathan", assigned="Echo", position=4)
AgentTaskCommand.objects.create(agent=a, task=a.tasks.get(ext_id="t4"), kind="dispatch",
                                status="pending", created_by="jonathan@dimagi.com")
# An applied command carries the outcome Echo recorded — surfaced on the card's
# "last:" line and in the activity stream.
AgentTaskCommand.objects.create(
    agent=a, task=a.tasks.get(ext_id="t5"), kind="done", status="applied",
    created_by="jonathan@dimagi.com", result_note="Shipped the agent workspace board.",
    applied_at=dt.datetime(2026, 6, 17, 14, 30, tzinfo=dt.timezone.utc))

a.syncs.all().delete()
a.work_products.all().delete()
a.skills.all().delete()
AgentSync.objects.create(
    agent=a, period_start=dt.datetime(2026, 6, 3, tzinfo=dt.timezone.utc),
    period_end=dt.datetime(2026, 6, 17, tzinfo=dt.timezone.utc), title="Manager sync 1",
    summary="First two weeks.", doc_url="https://docs.google.com/document/d/syncdoc/edit",
    self_grades={"work": "C+", "skills": "B-"}, source="manager-sync")
AgentWorkProduct.objects.create(
    agent=a, title="Demo story RUWOYD", kind="story",
    url="https://docs.google.com/document/d/wp1/edit", description="A 5/5 demo story.",
    tags=["story"], source="story-draft")
AgentSkill.objects.create(
    agent=a, name="email-communicator", description="Send and receive email as Echo.",
    url="https://github.com/dimagi-internal/echo/blob/main/skills/email-communicator/SKILL.md")
# A launchable skill so the phone composer has a human entry point to render; the
# one above stays non-launchable so the composer's filter has something to drop.
AgentSkill.objects.create(
    agent=a, name="story-ideation", description="Generate Connect story ideas.",
    launchable=True, args_hint="topic (optional)",
    url="https://github.com/dimagi-internal/echo/blob/main/skills/story-ideation/SKILL.md")

# An open emdash session the runner reported — drives /supervisor's Open sessions
# section. status ONLINE + a fresh heartbeat so GET /api/harness/sessions includes it.
from apps.harness.models import Runner
from apps.canopy_sessions.models import Session as CanopySession, RunnerBinding
from django.utils import timezone as _tz
_runner = Runner.objects.create(
    name="e2e-mbp", kind=Runner.EMDASH, host="e2e-host", paired_by=user, workspace=ws,
    status=Runner.ONLINE, last_heartbeat_at=_tz.now(), capabilities={"projects": ["canopy-web"]},
    ready=False, ready_note="emdash CDP unreachable",
)
# harness.EmdashSession was absorbed into canopy_sessions: the session itself, plus a
# RunnerBinding holding the live runner pointer (`emdash_task` → `session_key`). This
# seed still imported the deleted model, which raised at import time and took the WHOLE
# e2e suite down — every spec, not just the supervisor one. That is why nothing caught
# the items view growing to 32 cards.
_session = CanopySession.objects.create(
    workspace=ws, project="canopy-web",
    status=CanopySession.ACTIVE, origin=CanopySession.ORIGIN_RUNNER,
)
RunnerBinding.objects.create(
    session=_session, runner=_runner, session_key="cloud-runner",
    status="in_progress", last_interacted_at=_tz.now(),
)

# A fleet-audit findings review — a run-child gate whose run_id is NOT a DDD run id
# (nothing else in the system references it). It must render standalone: no DDD rail,
# and no narrative conjured out of its run_id. Mirrors what Ada posts.
ReviewRequest.objects.filter(run_id=FLEET_AUDIT_RUN_ID).delete()
fleet_audit = ReviewRequest.objects.create(
    owner=user,
    workspace=ws,
    run_id=FLEET_AUDIT_RUN_ID,
    narrative_slug=None,  # create_review stores NULL for a run-child gate
    version=0,
    gate="product_findings",
    visibility="link",
    request_json={
        "run_id": FLEET_AUDIT_RUN_ID,
        "gate": "product_findings",
        "iteration": 1,
        "clusters": [
            {
                "id": "hal-inbox",
                "title": "hal: discard 81 junk/stale unread emails (of 82 total)",
                "severity": "high",
                "fix_kind": "mechanical",
                "suggested_fix": "All 81 are automated or older than 1 week.",
            }
        ],
    },
)

# Ada's fleet audit as ITEMS — the surface that replaces the borrowed DDD review
# page. Two open items in her queue, both dispatching to another agent (the
# manager case: target_agent != self). hal must exist for dispatch to resolve.
ada_agent, _ = Agent.objects.update_or_create(slug="ada", defaults=dict(
    name="Ada", email="ada@dimagi-ai.com", description="Fleet conductor.",
    persona="Conducts the fleet.", workspace=ws))
Agent.objects.update_or_create(slug="hal", defaults=dict(
    name="Hal", email="hal@dimagi-ai.com", description="Inbox agent.",
    persona="Triages email.", workspace=ws))
Item.objects.filter(agent=ada_agent).delete()
Item.objects.create(
    agent=ada_agent, kind="review", origin="api", batch_key=FLEET_AUDIT_BATCH,
    idempotency_key="fa-hal-inbox", title="hal: discard 81 junk/stale unread emails",
    body="All 81 are automated or older than 1 week.",
    dispatch=[{"target_agent": "hal", "prompt": "/hal:turn", "origin": "email"}],
)
Item.objects.create(
    agent=ada_agent, kind="review", origin="api", batch_key=FLEET_AUDIT_BATCH,
    idempotency_key="fa-lily", title="hal: ONE buried HUMAN email — Lily Olson",
    body="A real person who never got an answer.",
    dispatch=[{"target_agent": "hal", "prompt": "/hal:turn --thread lily", "origin": "email"}],
)
# A QUESTION item — the other kind. Its card is the only thing that renders the
# answer input, and without one seeded the placeholder-contrast guard had nothing
# to inspect and passed while the bug was live. (Verified: reintroducing the
# invisible-placeholder class now turns that test red.)
Item.objects.create(
    agent=ada_agent, kind="question", origin="api", batch_key=FLEET_AUDIT_BATCH,
    idempotency_key="fa-question", title="hal: should the 81 be archived or deleted?",
    body="Archiving is reversible; deleting is not.",
    dispatch=[{"target_agent": "hal", "prompt": "/hal:turn", "origin": "email"}],
)
# A settled card from an OLDER sitting. Items are never deleted, so this is what
# accumulates: the unfiltered view must keep it out of the way of the open ones.
# Deliberately in a different batch, so the batch-permalink tests don't see it.
Item.objects.create(
    agent=ada_agent, kind="review", origin="api", batch_key="fleet-audit-2026-06-30",
    idempotency_key="fa-old-settled", title="hal: an old finding nobody needs to see again",
    body="Long since dismissed.", state=Item.DISMISSED,
)

session = SessionStore()
session["_auth_user_id"] = str(user.pk)
session["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
session["_auth_user_hash"] = user.get_session_auth_hash()
session.set_expiry(24 * 3600)
session.save()

os.makedirs("frontend/e2e/.auth", exist_ok=True)
with open("frontend/e2e/.auth/session.txt", "w") as f:
    f.write(session.session_key)

print(f"seeded: {a.tasks.count()} tasks, {a.commands.filter(status='pending').count()} pending; "
      f"fleet-audit review {str(fleet_audit.id)[:8]}; session {session.session_key[:8]}")
