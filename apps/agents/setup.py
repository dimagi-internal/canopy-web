"""What is left before an agent can do its job — THE list, computed from real state.

One definition for every door: the agent's Settings page renders it, the create wizard
lands on it, and because every route is an MCP tool (`agent_setup`), a Claude Code session
finishing `/canopy:create-agent` can ask the same question instead of keeping its own copy.

Each step reads the predicate the system itself uses for that thing, never a second
opinion: GitHub is `delegations.readiness` (what a box's health check says), the runner is
the routing module's own order + `runner_may_hold_agent` + `runner_tenant_slugs` (what the
claim does), the mailbox is what the boxes that run its turns found (`bootstrap_reports`). A step that disagreed with
the real check would be the "screen says fine, turn fails" bug this exists to prevent.

`where` says which tool does it: "web" steps are a link to the control that does it;
"claude-code" steps are judgment work — a persona, the first real skill — that happens in a
session in the agent's repo, so the step carries the prompt that starts one; "terminal"
steps need a person's own access that canopy-web does not hold (the vault: 1Password
rights), so the step carries the one command that does it.
"""
from __future__ import annotations

from apps.harness.models import Runner, RunnerAssignment, Turn, agent_turns_q
from apps.harness.routing import inherited_orders, runner_tenant_slugs
from apps.harness.runners import request_refresh

from . import delegations, mailbox
from .credentials import zk_seal
from .credentials.oauth_api import GOG_TOKEN_REF
from .credentials.pat_rotation import CANOPY_PAT_REF
from .credentials.vault_health import vault_health
from .models import Agent, AgentBootstrapReport, AgentCredential
from .services import bootstrap_reports, runner_may_hold_agent

DONE, TODO, WAITING, OPTIONAL = "done", "todo", "waiting", "optional"


def scaffold_skill_names() -> set[str]:
    """Skills every factory-stamped agent ships with — from the factory's own stamp table,
    so a skill the factory adds later never reads as "the agent has a job"."""
    from canopy_agent_factory import templates

    return {p.split("/")[1] for p in templates() if p.startswith("skills/") and p.endswith("/SKILL.md")}


def _step(key, title, status, detail, *, where="web", href="", action="", command="", links=(),
          only_to_run_on_its_own=False):
    """`action` names what `href` does ("Add a GitHub token"): several steps link to
    the same Settings section, and a bare "Open" left a newcomer guessing. `links` are
    further `{href, action}` choices beside the main one.

    `only_to_run_on_its_own` marks a step needed only for the agent to run on its own
    (a runner: schedules, mail, dispatches), not for a person to use it in their own
    Claude Code (`canopy agent use <slug>`, docs/architecture/runners.md → "Two ways an
    agent runs"). It is a separate field, not a softer `status`: the status still says
    what is true of that step, so a reader that counts todo steps (canopy's
    agent_doctor) reads the same answer it always did; the Settings checklist uses the
    field to call an agent used only in Claude Code complete."""
    return {"key": key, "title": title, "status": status, "detail": detail,
            "where": where, "href": href, "action": action, "command": command,
            "links": list(links), "only_to_run_on_its_own": only_to_run_on_its_own}


#: Pairing a new runner, for the runner step when nothing can take the agent. A laptop
#: pairs with one command (`install-runner.sh`, on /get-started); a cloud box is stood
#: up from the EC2 runner's README — that needs AWS rights canopy-web does not hold.
PAIR_LAPTOP = {"href": "/get-started#runner", "action": "Pair your laptop"}
PAIR_CLOUD_BOX = {
    "href": "https://github.com/dimagi-internal/canopy-web/blob/main/runner/ec2/README.md"
            "#lifecycle--commands-only",
    "action": "Stand up a cloud box",
}


def _github(agent: Agent, settings: str) -> dict:
    verdict, detail = delegations.readiness(agent)
    owner = agent.owner.email if agent.owner_id else ""
    return _step(
        "github", "Lend it a GitHub identity",
        DONE if verdict in ("ok", "warn") else TODO,
        detail[:1].upper() + detail[1:] if verdict != "fail" else
        (f"Its turns push code and open pull requests as whoever lends it a GitHub token: its "
         f"owner, {owner}, or one of its admins.") if agent.owner_id else
        "Its turns push code and open pull requests as whoever lends it a GitHub token, and it "
        "has no owner yet. A workspace owner can add the token, or give it an owner under "
        "People and roles.",
        href=f"{settings}#cred-github", action="Add a GitHub token")


def _mailbox(agent: Agent, settings: str) -> dict:
    if not agent.email:
        return _step("mailbox", "Connect its mailbox", OPTIONAL,
                     "No mailbox set. Email is optional; add one later if it should send or read mail.")

    box = mailbox.describe(agent)
    if box["kind"] == "hosted":
        # Canopy-hosted: nothing to sign in. Mail to the address is the agent's turn once
        # its route exists and the domain is verified; the workspace email page is where
        # either gets fixed.
        email_page = f"/w/{agent.workspace_id}/settings/email"
        if box["live"]:
            return _step("mailbox", "Connect its mailbox", DONE,
                         f"{agent.email} is canopy-hosted email — nothing to sign in. Mail to it "
                         "becomes this agent's turn.", href=email_page, action="Email settings")
        if box["route"] == "taken":
            why = f"{agent.email} already goes somewhere else; route it to this agent"
        elif not box["verified"]:
            why = f"{box['domain']} is still waiting for its DNS records"
        else:
            why = f"{agent.email} has no mailbox route yet"
        return _step("mailbox", "Connect its mailbox", TODO,
                     f"Canopy-hosted email: {why} on the workspace email page.",
                     href=email_page, action="Email settings")

    # What the boxes that run its turns found is the answer; the stored token is only
    # the fallback for an agent no box has checked yet. Before 2026-10-20 either one
    # alone made this "done", so lex read done while cloud-ec2-2 reported it could not
    # find the token at all: the checklist said green, the agent could not read mail.
    # (The token-OR-proved rule itself is right for tokens minted by a laptop
    # `gog login`, which live in the vault rather than here — ada/hal/eva/echo read
    # "todo" under a token-only rule while cloud-ec2-2 had them working, 2026-10-15.)

    reports = bootstrap_reports(agent)
    proved = next((r.runner_name for r in reports if r.turn_ready), "")
    failing = next((r for r in reports if not r.mailbox_ok and r.turn_ready is not True), None)
    has = AgentCredential.objects.filter(agent=agent, name=GOG_TOKEN_REF).exists()
    action = {"href": f"{settings}#credentials", "action": "Connect the mailbox"}
    if proved:
        return _step("mailbox", "Connect its mailbox", DONE,
                     f"Signed in as {agent.email}; {proved} reads it for turns.", **action)
    if failing is not None:
        why = _first_reason(failing.detail)
        return _step("mailbox", "Connect its mailbox", TODO,
                     f"{failing.runner_name} cannot read {agent.email}"
                     + (f": {why}" if why else ".")
                     + (" Its Google sign-in is stored here, so the box is not getting it;"
                        " connect the mailbox again if this does not clear on its next refresh."
                        if has else f" Sign {agent.email} in with Google."),
                     **action)
    if has:
        return _step("mailbox", "Connect its mailbox", DONE,
                     f"Signed in as {agent.email}. No machine has checked it yet.", **action)
    return _step("mailbox", "Connect its mailbox", TODO,
                 f"Sign {agent.email} in with Google so it can send and read mail.", **action)


def _first_reason(detail: str, limit: int = 160) -> str:
    """The first reason in a box's `;`-joined report detail, shortened for one line."""
    first = (detail or "").split(";")[0].strip()
    return first if len(first) <= limit else first[:limit - 1].rstrip() + "…"


def _vault(agent: Agent, settings: str) -> dict:
    """Where its secrets live — read from THE vault verdict (`vault_health`), so this
    step and the workspace health dashboard cannot disagree. An agent resolving from
    the canopy-web vault is judged by that vault (every declared secret stored, every
    machine let in); one resolving from 1Password by its 1Password vault and key."""

    if agent.credential_source == Agent.CANOPY_WEB:
        return _canopy_vault(agent, settings, vault_health(agent))
    return _op_vault(agent, settings)


def _canopy_login(agent: Agent, settings: str) -> dict | None:
    """Its own canopy login (`canopy-pat`) — one click on its Credentials page makes it,
    stores it sealed and turns on renewal. Only for an agent on the Canopy vault; a
    1Password agent's login comes from its vault like every other secret."""
    if agent.credential_source != Agent.CANOPY_WEB:
        return None

    has = AgentCredential.objects.filter(agent=agent, name=CANOPY_PAT_REF).exists()
    return _step(
        "canopy_login", "Give it its canopy login", DONE if has else TODO,
        ("It signs in to canopy with its own login"
         + (", which renews itself." if agent.pat_rotation_enabled else ".")) if has else
        "How it reads its tasks, sends its email and reports back. One click makes it and keeps "
        "it in its vault; it renews itself.",
        href=f"{settings}#canopy-login", action="See its login" if has else "Give it its canopy login")


def _canopy_vault(agent: Agent, settings: str, vh: dict) -> dict:

    title = "Give it a vault"
    if agent.zk_enabled:

        if not zk_seal.has_recipients(agent):
            # Every value is sealed to the vault's readers (its approved runners'
            # device keys, its admins' vault keys). With none, nothing can be stored —
            # connecting Google ends in `zk-no-recipients` — so this comes first.
            return _step("vault", title, TODO,
                         "Nobody can open its vault yet, so it cannot store a secret — Google "
                         "included. Set up your vault key with a passkey (or approve one of its "
                         "runners' device keys) first.",
                         href="/settings#vault-key", action="Set up your vault key")
    # Its canopy login has its own step (and button); listing it here too would be
    # one missing thing reported twice.
    leftover = [n for n in vh["in_1password"] + vh["missing"] if n != CANOPY_PAT_REF]
    vh = {**vh, "in_1password": [n for n in vh["in_1password"] if n != CANOPY_PAT_REF],
          "missing": [n for n in vh["missing"] if n != CANOPY_PAT_REF]}
    if leftover:
        parts = []
        if vh["in_1password"]:
            parts.append(f"still only in 1Password: {', '.join(vh['in_1password'])}")
        if vh["missing"]:
            parts.append(f"not stored anywhere: {', '.join(vh['missing'])}")
        return _step("vault", title, TODO,
                     f"It reads its secrets from canopy-web, but {'; '.join(parts)}. Store each "
                     "one here so its runners can read it.",
                     href=f"{settings}#credentials", action="Add the missing secrets")
    if vh["machines_waiting"]:
        names = ", ".join(vh["machines_waiting"])
        return _step("vault", title, WAITING,
                     f"Its secrets are in canopy-web, but {names} cannot open them yet. One of its "
                     "admins lets each machine in with a passkey touch.",
                     href="/settings#your-agents", action="Give access")
    return _step("vault", title, DONE,
                 f"All {vh['declared']} declared secrets are in canopy-web." if vh["declared"] else
                 "It reads secrets from canopy-web and declares none.",
                 href=f"{settings}#credentials", action="See its secrets")


def _op_vault(agent: Agent, settings: str) -> dict:
    """The agent's 1Password vault and the key a runner reads it with — and, once a box
    has used them, whether its secrets actually materialized (`env_ok`, the box's own
    `op inject`). Recorded-but-broken reads as todo with the box's reason: a key that is
    stored but cannot read the vault is the 2026-09-22 "four agents, two weeks, only in
    journald" failure.

    Creating a vault and minting its key need a PERSON's 1Password rights, which neither
    canopy-web nor a box has, so this step is a terminal command, not a link. One command
    does all of it and is safe to re-run: `canopy agent vault` (canopy repo)."""

    command = f"canopy agent vault --slug {agent.slug} --workspace {agent.workspace_id}"
    if not agent.op_vault:
        # The person creating it names the vault; it is never derived from the slug.
        command += " --vault <vault name>"
    title = "Give it a vault"
    if not agent.op_vault or not agent.op_sa_token_enc:
        state = (f"Its vault, {agent.op_vault}, has no key stored here, so no runner can read it."
                 if agent.op_vault else "It has no 1Password vault yet.")
        return _step(
            "vault", title, TODO,
            f"{state} Someone with 1Password rights runs this once (naming the vault, if it has "
            "none): it creates the vault, mints "
            "what can be minted, asks for what only a person knows, and stores a read-only key "
            "here for its runners.",
            where="terminal", command=command)
    failed = list(AgentBootstrapReport.objects.filter(agent=agent, env_ok=False)
                  .values_list("runner_name", "detail"))
    if failed:
        name, why = failed[0]
        return _step(
            "vault", title, TODO,
            f"{name} could not read its secrets from {agent.op_vault}"
            + (f": {why}" if why else "") + ". Re-run the command to check every value.",
            where="terminal", command=command)
    return _step("vault", title, DONE, f"Secrets in {agent.op_vault}; its runners hold a key for it.",
                 where="terminal", command=command)


def runner_takers(agent: Agent):
    """(takers, inherited): the live-or-not boxes that would take this agent's
    turns — its own default order, else the workspace order it follows, narrowed by
    the claim's own predicates (tenant, `runner_may_hold_agent`) — and the
    inherited order, for why a listed box was dropped. Shared by the checklist and
    by registration, which asks these boxes to set a new agent up."""

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

    asked = []
    for r in runner_takers(agent)[0]:
        if r.kind == Runner.CLOUD:
            request_refresh(r)
            asked.append(r.name)
    return asked


def _runner(agent: Agent, settings: str, viewer_is_admin: bool = False) -> dict:

    takers, inherited = runner_takers(agent)
    # A cloud box serves an agent only once its bootstrap has cloned it, and every
    # bootstrap reports per agent — so no report from that box means it has not yet.
    reported = set(AgentBootstrapReport.objects.filter(agent=agent).values_list("runner_name", flat=True))
    ready = [r for r in takers if r.kind != Runner.CLOUD or r.name in reported]
    unset = [r for r in takers if r not in ready]
    online = [r for r in ready if r.live_status == Runner.ONLINE]
    title = "Give it a runner, to run on its own"
    # Every not-done answer says the other way too: a person can use the agent in
    # their own Claude Code with no runner at all.
    yourself = (f" Not needed to use {agent.name} yourself in Claude Code "
                f"(canopy agent use {agent.slug}).")

    def step(status, detail, **kw):
        return _step("runner", title, status, detail + ("" if status == DONE else yourself),
                     only_to_run_on_its_own=True, **kw)

    if online:
        return step(DONE, f"{online[0].name} takes its turns.",
                    href=f"{settings}#running", action="See routing")
    if unset:
        names = ", ".join(r.name for r in unset)
        return step(WAITING,
                    f"{names} will take its turns once it has set {agent.name} up. It does that on "
                    "its own when it next has no turn running, which can take a while if it is "
                    "busy; this ticks when it reports back.",
                    href=f"{settings}#running", action="See routing")
    if takers:
        names = ", ".join(r.name for r in takers)
        return step(WAITING, f"{names} would take its turns, but none is online.",
                    href=f"{settings}#running", action="See routing")
    why = []
    if inherited and inherited.missing_repo:
        why.append(f"{', '.join(r.name for r in inherited.missing_repo)} does not have its repo "
                   "checked out yet")
    if inherited and inherited.cannot_hold:
        why.append(f"{', '.join(r.name for r in inherited.cannot_hold)} belongs to someone who is "
                   "not one of its admins")
    if inherited and inherited.directed:
        why.append(f"{', '.join(r.name for r in inherited.directed)} takes only work put on it "
                   "(directed only)")
    detail = ("; ".join(why).capitalize() + "." if why else
              "A runner is the computer that runs its turns on its own (schedules, mail, "
              "dispatches), and none is set to take its work. Pick one, or pair a runner of "
              "your own first.")
    # Pairing is offered only to someone whose own box could then hold the agent: a
    # runner runs an agent only when its owner is one of the agent's admins
    # (`runner_may_hold_agent`), so anyone else's new box would still take nothing.
    return step(TODO, detail, href=f"{settings}#running", action="Pick a runner",
                links=[PAIR_LAPTOP, PAIR_CLOUD_BOX] if viewer_is_admin else [])


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
        f"/canopy:create-agent for {agent.slug} at Step 4's `job` step: fill in persona.md "
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

    # Its turns include those that reach it through a chat (email, Slack), which
    # carry the agent on their session — the same set its Turns page lists.
    done = Turn.objects.filter(agent_turns_q(agent), status=Turn.DONE).exists()
    return _step("first_turn", "Run its first turn", DONE if done else TODO,
                 "It has finished a turn." if done else
                 "Give it something to do from its Tasks page and watch the turn finish.",
                 # The prompt box (`QuickTurn`) is on Tasks, not the Projects landing page.
                 href=f"{base}/turns" if done else f"{base}/tasks",
                 action="See its turns" if done else "Ask it something")


def setup_steps(agent: Agent, *, viewer_is_admin: bool = False) -> list[dict]:
    """The ordered checklist. Links are paths inside the agent's workspace.
    `viewer_is_admin` (`agent.is_admin(viewer)`, which the caller has usually asked
    already) adds the choices only an admin can act on (pairing a runner)."""
    base = f"/w/{agent.workspace_id}/agents/{agent.slug}"
    settings = f"{base}/settings"
    repo = delegations.agent_repo(agent)
    mailbox = _mailbox(agent, settings)
    vault = [_vault(agent, settings), *[s for s in [_canopy_login(agent, settings)] if s]]
    # A zero-knowledge agent on the canopy-web vault seals what Google hands back to
    # whoever can open its vault, so the vault comes first; otherwise the mint fails
    # with `zk-no-recipients`.
    zk_first = agent.zk_enabled and agent.credential_source == Agent.CANOPY_WEB
    return [
        _step("repo", "Give it a repository", DONE if agent.repo_url else TODO,
              f"Code lives in {repo or agent.repo_url}." if agent.repo_url else
              "It has no repository, so a runner has nothing to run. Point it at the one "
              "it already has, or make an empty one on GitHub first.",
              # Its own field on Settings — never the create wizard, which makes a
              # whole new agent.
              href=agent.repo_url or f"{settings}#repository",
              action="Open on GitHub" if agent.repo_url else "Set its repository"),
        _github(agent, settings),
        *([*vault, mailbox] if zk_first else [mailbox, *vault]),
        _runner(agent, settings, viewer_is_admin),
        _job(agent),
        _first_turn(agent, base),
    ]
