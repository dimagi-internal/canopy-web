"""Readiness drills — a hard-pinned, read-only doctor turn per (runner, agent).

A drill is resolved by the drilled agent's own report callback (proving it can
reach the control plane, not just run its checks locally) or by its turn failing
outright. `enqueue_turn` stays in `services.py` and is imported where it is
used, because `services.py` imports this module. Split out of `services.py`,
which still re-exports every name here.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.utils import timezone

from .models import (
    Runner,
    RunnerDrill,
    Turn,
)

# ---------------------------------------------------------------------------
# Readiness drills — a hard-pinned, read-only doctor turn per (runner, agent),
# resolved by the drilled agent's own report callback (proving it can reach
# the control plane) or by the turn failing outright. See
# docs/superpowers/specs/2026-07-24-directed-runner-routing-design.md.
# ---------------------------------------------------------------------------

DRILL_PROMPT = """READINESS CHECK — READ-ONLY. You are the agent "{agent_slug}".
Verify you can operate end-to-end in THIS environment, then report.

1. Confirm your working environment. If your agent repo is not checked out here,
   clone it (this turn carries your owner's GitHub token for you, in GH_TOKEN).
2. Run your doctor / preflight / setup-verification checks. READ-ONLY mode:
   take NO outward action — no emails, no posts, no board writes, no deploys,
   no state mutations anywhere. The ONE exception is the report in step 3, which
   is not an outward action at all: it is this drill's return value.

   Include this GitHub check, which proves you can SHIP, not merely that `gh` is
   logged in. It asks GitHub to open a pull request from a branch that does not
   exist, so it creates nothing:

   {github_check}
3. Report the result. THIS STEP IS MANDATORY AND ALREADY AUTHORIZED — it is how a
   drill returns its answer to the system that asked for it, the same way any
   other turn ends by reporting. It is not a message to anyone, it writes no
   agent state, and it needs no approval. Do NOT stop to request one: this turn
   runs unattended, so there is nobody to grant it, and a drill that ends without
   reporting is indistinguishable from a box that could not reach the control
   plane — which is the exact failure the drill exists to detect.

   Report as YOURSELF: your own canopy token comes first, and the operator's
   workbench token is only a fallback for an agent that has no login of its own.

   TOKEN="${{CANOPY_WEB_PAT:-$(sed -n 's/^CANOPY_WEB_PAT=//p' ~/.{agent_slug}/.env 2>/dev/null | head -1)}}"
   TOKEN="${{TOKEN:-$(cat ~/.claude/canopy/workbench-token 2>/dev/null || echo "${{CANOPY_TOKEN:-$CANOPY_PAT}}")}}"
   curl -s -X POST "{report_url}" \\
     -H "Authorization: Bearer $TOKEN" \\
     -H "Content-Type: application/json" \\
     -d '{{"outcome": "pass", "summary": "<one-paragraph findings>"}}'

   Use "outcome": "fail" if ANY check failed, and say which. Report whatever you
   found, including a failure you could not fix — reporting a bad result is the
   drill succeeding, not the drill failing. Keep the summary to one paragraph. Do
   nothing after reporting."""


def _drill_github_check(agent) -> str:
    """The drill's GitHub step for `agent`. 422 = may open pull requests there
    (GitHub checks permission before it validates the branch); 403 = the token
    lacks Pull requests: write; 404 = it cannot see the repo at all."""
    from apps.agents.delegations import agent_repo

    repo = agent_repo(agent)
    if not repo:
        return "(this agent has no GitHub repo recorded in canopy-web — say so in the report)"
    return (
        f"code=$(curl -s -o /dev/null -w '%{{http_code}}' -X POST "
        f"-H \"Authorization: Bearer $GH_TOKEN\" https://api.github.com/repos/{repo}/pulls "
        f"-d '{{\"title\":\"canopy readiness probe\",\"head\":\"canopy-drill-probe/does-not-exist\","
        f"\"base\":\"main\"}}')\n"
        f"   422 = PASS (can open pull requests on {repo}); 403 = FAIL (token lacks Pull "
        f"requests: write); 404 = FAIL (token cannot see {repo}); empty GH_TOKEN = FAIL "
        f"(no GitHub identity was issued to this turn).\n"
        f"   Then push, the same way — a branch at a commit that cannot exist, so nothing "
        f"is created:\n"
        f"   code=$(curl -s -o /dev/null -w '%{{http_code}}' -X POST "
        f"-H \"Authorization: Bearer $GH_TOKEN\" https://api.github.com/repos/{repo}/git/refs "
        f"-d '{{\"ref\":\"refs/heads/canopy-drill-probe/does-not-exist\","
        f"\"sha\":\"0000000000000000000000000000000000000001\"}}')\n"
        f"   422 = PASS (can push to {repo}); 403 or 404 = FAIL (token lacks Contents: write "
        f"on {repo})."
    )


#: A readiness check's report link carries its own permission: whoever holds
#: the link may report THAT run's result, and nothing else. The run is named by
#: (drill id, started_at) because the row is reused per (runner, agent) — an old
#: link must not be able to answer a newer run.
_DRILL_REPORT_SALT = "harness.drill-report"
_DRILL_REPORT_MAX_AGE = 2 * 24 * 3600


def drill_report_token(drill: RunnerDrill) -> str:
    from django.core import signing

    return signing.dumps({"d": drill.pk, "s": drill.started_at.isoformat()},
                         salt=_DRILL_REPORT_SALT)


def drill_report_token_ok(drill: RunnerDrill, token: str) -> bool:
    from django.core import signing

    if not token or drill.started_at is None:
        return False
    try:
        body = signing.loads(token, salt=_DRILL_REPORT_SALT, max_age=_DRILL_REPORT_MAX_AGE)
    except signing.BadSignature:
        return False
    return body == {"d": drill.pk, "s": drill.started_at.isoformat()}


def _drill_initiator(runner, started_by=None):
    from . import initiator as who
    return who.system(via="drill", accountable=started_by or runner.owner)


def start_drill(runner: Runner, agents: list, *, started_by=None) -> list[RunnerDrill]:
    """Fan a readiness drill out over `agents`: reset each (runner, agent)
    RunnerDrill to pending and enqueue one hard-pinned, read-only doctor turn
    per agent. Drills queue behind real executing turns (the one-executing-turn
    constraint) — they never interrupt live work.

    `started_by` is the person who asked (the runner's owner or one of its
    admins); it is recorded as the turn's accountable person, so they can read
    the drill turn they started. The caller has already checked who may drill
    which agent (`api.start_runner_drill`)."""
    from .services import enqueue_turn  # services.py imports this module

    drills: list[RunnerDrill] = []
    for agent in agents:
        drill, _ = RunnerDrill.objects.update_or_create(
            runner=runner, agent=agent,
            defaults={"outcome": RunnerDrill.OUTCOME_PENDING, "summary": "",
                      "finished_at": None, "started_at": timezone.now()},
        )
        # The link authorizes the report, not the reporter's identity: an agent
        # reporting as its own canopy login was 404'd whenever that login was
        # not linked to the agent row (echo/ace/eva, 2026-09-26), and a drill
        # that cannot report reads exactly like a box that cannot reach canopy.
        report_url = (f"{settings.CANOPY_PUBLIC_BASE_URL}/api/harness/drills/{drill.id}/report"
                      f"?t={drill_report_token(drill)}")
        turn, _created = enqueue_turn(
            agent=agent,
            origin=Turn.ORIGIN_API,
            idempotency_key=f"drill:{runner.id}:{agent.slug}:{uuid.uuid4().hex[:8]}",
            prompt=DRILL_PROMPT.format(agent_slug=agent.slug, report_url=report_url,
                                       github_check=_drill_github_check(agent)),
            pinned_runner=runner,
            # A readiness drill is canopy checking a box; whoever started it
            # (the runner's owner by default) is the person it is run for.
            initiator=_drill_initiator(runner, started_by),
        )
        drill.turn = turn
        drill.save(update_fields=["turn"])
        drills.append(drill)
    return drills


def report_drill(drill: RunnerDrill, *, outcome: str, summary: str) -> RunnerDrill:
    """The drilled agent's own callback — proves this environment can reach the
    control plane, not just run its checks locally."""
    if outcome not in (RunnerDrill.OUTCOME_PASS, RunnerDrill.OUTCOME_FAIL):
        raise ValueError(f"outcome must be pass|fail, got {outcome!r}")
    drill.outcome = outcome
    drill.summary = summary
    drill.finished_at = timezone.now()
    drill.save(update_fields=["outcome", "summary", "finished_at"])
    return drill
