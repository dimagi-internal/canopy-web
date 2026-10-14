"""What is left before an agent can do its job — THE list, computed from real state.

One definition for every door: the agent's Settings page renders it, the create wizard
lands on it, and because every route is an MCP tool (`agent_setup`), a Claude Code session
finishing `/canopy:create-agent` can ask the same question instead of keeping its own copy.

Each step reads the predicate the system itself uses for that thing, never a second
opinion: GitHub is `delegations.readiness` (what a box's health check says), the runner is
the routing module's own order + `runner_may_hold_agent` + `runner_tenant_slugs` (what the
claim does), the mailbox is the stored `gog-token` credential. A step that disagreed with
the real check would be the "screen says fine, turn fails" bug this exists to prevent.

`where` says which tool does it: "web" steps are a link to the control that does it;
"claude-code" steps are judgment work — a persona, the first real skill — that happens in a
session in the agent's repo, so the step carries the prompt that starts one.
"""
from __future__ import annotations

from . import delegations
from .models import Agent, AgentCredential
from .oauth_api import GOG_TOKEN_REF

DONE, TODO, WAITING, OPTIONAL = "done", "todo", "waiting", "optional"


def scaffold_skill_names() -> set[str]:
    """Skills every factory-stamped agent ships with — from the factory's own stamp table,
    so a skill the factory adds later never reads as "the agent has a job"."""
    from canopy_agent_factory import templates

    return {p.split("/")[1] for p in templates() if p.startswith("skills/") and p.endswith("/SKILL.md")}


def _step(key, title, status, detail, *, where="web", href="", action="", command=""):
    """`action` names what `href` does ("Add a GitHub token"): several steps link to
    the same Settings section, and a bare "Open" left a newcomer guessing."""
    return {"key": key, "title": title, "status": status, "detail": detail,
            "where": where, "href": href, "action": action, "command": command}


def _github(agent: Agent, settings: str) -> dict:
    verdict, detail = delegations.readiness(agent)
    owner = agent.owner.email if agent.owner_id else "its owner"
    return _step(
        "github", "Lend it a GitHub identity",
        DONE if verdict in ("ok", "warn") else TODO,
        detail[:1].upper() + detail[1:] if verdict != "fail" else
        f"Its turns push code and open pull requests as its owner, {owner}. Only the owner "
        "can add that GitHub token.",
        href=f"{settings}#credentials", action="Add a GitHub token")


def _mailbox(agent: Agent, settings: str) -> dict:
    if not agent.email:
        return _step("mailbox", "Connect its mailbox", OPTIONAL,
                     "No mailbox set. Email is optional; add one later if it should send or read mail.")
    has = AgentCredential.objects.filter(agent=agent, name=GOG_TOKEN_REF).exists()
    return _step(
        "mailbox", "Connect its mailbox", DONE if has else TODO,
        f"Signed in as {agent.email}." if has else
        f"Sign {agent.email} in with Google so it can send and read mail.",
        href=f"{settings}#credentials", action="Connect the mailbox")


def runner_takers(agent: Agent):
    """(takers, inherited): the live-or-not boxes that would take this agent's
    turns — its own default order, else the workspace order it follows, narrowed by
    the claim's own predicates (tenant, `runner_may_hold_agent`) — and the
    inherited order, for why a listed box was dropped. Shared by the checklist and
    by registration, which asks these boxes to set a new agent up."""
    from apps.harness.models import Runner, RunnerAssignment
    from apps.harness.routing import inherited_orders, runner_tenant_slugs
    from .services import runner_may_hold_agent

    own = [a.runner for a in RunnerAssignment.objects.filter(agent=agent, source="", enabled=True)
           .select_related("runner", "runner__owner")]
    inherited = inherited_orders([agent.id]).get(agent.id)
    listed = own or (inherited.runners if inherited else [])
    takers = [r for r in listed if r.status != Runner.RETIRED
              and agent.workspace_id in runner_tenant_slugs(r) and runner_may_hold_agent(r, agent)]
    return takers, inherited


def request_setup(agent: Agent) -> list[str]:
    """Ask each CLOUD box that would take this agent's turns to re-run its bootstrap,
    which clones and provisions every registered agent — the only time a cloud box
    learns of a new one. Without this a new agent's turns ran on the box with none
    of its code (seen on labs 2026-10-14: the turn answered with the fleet's skills).
    Durable (`request_refresh` sets a row the box reads off its heartbeat) and acted
    on at the box's next idle moment. Laptops are not asked: their checkout is a
    person's to make (`repoSetup.ts`)."""
    from apps.harness.models import Runner
    from apps.harness.runners import request_refresh

    asked = []
    for r in runner_takers(agent)[0]:
        if r.kind == Runner.CLOUD:
            request_refresh(r)
            asked.append(r.name)
    return asked


def _runner(agent: Agent, settings: str) -> dict:
    from apps.harness.models import Runner

    from .models import AgentBootstrapReport

    takers, inherited = runner_takers(agent)
    # A cloud box serves an agent only once its bootstrap has cloned it, and every
    # bootstrap reports per agent — so no report from that box means it has not yet.
    reported = set(AgentBootstrapReport.objects.filter(agent=agent).values_list("runner_name", flat=True))
    ready = [r for r in takers if r.kind != Runner.CLOUD or r.name in reported]
    unset = [r for r in takers if r not in ready]
    online = [r for r in ready if r.live_status == Runner.ONLINE]
    title = "Give it a runner"
    if online:
        return _step("runner", title, DONE, f"{online[0].name} takes its turns.",
                     href=f"{settings}#running", action="See routing")
    if unset:
        names = ", ".join(r.name for r in unset)
        return _step("runner", title, WAITING,
                     f"{names} will take its turns once it has set {agent.name} up. It does that on "
                     "its own when it next has no turn running, which can take a while if it is "
                     "busy; this ticks when it reports back.",
                     href=f"{settings}#running", action="See routing")
    if takers:
        names = ", ".join(r.name for r in takers)
        return _step("runner", title, WAITING, f"{names} would take its turns, but none is online.",
                     href=f"{settings}#running", action="See routing")
    why = []
    if inherited and inherited.missing_repo:
        why.append(f"{', '.join(r.name for r in inherited.missing_repo)} does not have its repo "
                   "checked out yet")
    if inherited and inherited.cannot_hold:
        why.append(f"{', '.join(r.name for r in inherited.cannot_hold)} belongs to someone who is "
                   "not one of its admins")
    detail = ("; ".join(why).capitalize() + "." if why else
              "A runner is the computer that runs its turns, and none is set to take its work. "
              "Pick one, or pair a runner of your own first.")
    return _step("runner", title, TODO, detail, href=f"{settings}#running", action="Pick a runner")


#: Where a laptop runner expects an agent's checkout (runner/canopy_runner/README.md;
#: the frontend's `repoSetup.ts` hands out the same path). Cloning there for the
#: persona session means the same checkout is the one a laptop runner later runs.
def _checkout(slug: str) -> str:
    return f"~/emdash-projects/{slug}"


def _job(agent: Agent) -> dict:
    custom = set(agent.skills.values_list("name", flat=True)) - scaffold_skill_names()
    folder = _checkout(agent.slug)
    command = (
        f"git clone {agent.repo_url} {folder} && cd {folder} && claude \"Continue "
        f"/canopy:create-agent for {agent.slug} from Step 3 (Make it real): fill in persona.md "
        "with me, write its first domain skill, add a deny rail for each outbound action, then "
        "run canopy agent doctor and publish it.\""
    ) if agent.repo_url else ""
    return _step(
        "job", "Give it a job", DONE if custom else TODO,
        f"Skills: {', '.join(sorted(custom))}." if custom else
        "A persona and its first real skill. This is judgment work, done with Claude Code in "
        "the agent's repo — the scaffold only has the operating model.",
        where="claude-code", command=command)


def _first_turn(agent: Agent, base: str) -> dict:
    from apps.harness.models import Turn

    done = Turn.objects.filter(agent=agent, status=Turn.DONE).exists()
    return _step("first_turn", "Run its first turn", DONE if done else TODO,
                 "It has finished a turn." if done else
                 "Give it something to do from its Tasks page and watch the turn finish.",
                 # The prompt box (`QuickTurn`) is on Tasks, not the Projects landing page.
                 href=f"{base}/turns" if done else f"{base}/tasks",
                 action="See its turns" if done else "Ask it something")


def setup_steps(agent: Agent) -> list[dict]:
    """The ordered checklist. Links are paths inside the agent's workspace."""
    base = f"/w/{agent.workspace_id}/agents/{agent.slug}"
    settings = f"{base}/settings"
    repo = delegations.agent_repo(agent)
    return [
        _step("repo", "Create its repository", DONE if agent.repo_url else TODO,
              f"Code lives in {repo or agent.repo_url}." if agent.repo_url else
              "It has no repository, so a runner has nothing to run.",
              href=agent.repo_url or f"/w/{agent.workspace_id}/agents/new",
              action="Open on GitHub" if agent.repo_url else "Create it"),
        _github(agent, settings),
        _mailbox(agent, settings),
        _runner(agent, settings),
        _job(agent),
        _first_turn(agent, base),
    ]
