"""Several runners on one machine: the two refusals that keep a runner to its one agent (docs/architecture/runners.md). A leaf of the routing modules; import these names from `routing`.
"""
from __future__ import annotations

from .models import (
    Runner,
    serves_of,
)

# ---- several runners on one machine (docs/architecture/runners.md) -----------
#
# A cloud machine can run several runners, each serving a workspace tree or a
# single agent that has a runner of its own. Each runner is its own Unix user with
# its own keys, so an agent's secrets reach only the runner that runs it. Two
# refusals keep routing inside that: an agent's runner runs its one agent, and no OTHER runner on
# the same machine (`Runner.machine_id`) runs that agent. Both are restrictions
# a box declares on itself, so trusting its declaration is safe.

#: The attribute `prime_machine_runners` caches a runner's machine siblings that run
#: one agent on: {agent slug: sibling runner name}.
_MACHINE_AGENT_RUNNERS = "_machine_agent_runners"


def _served_agent_slug(serves: dict) -> str | None:
    """The agent an agent's runner runs ("" when it names none); None: not one agent's runner."""
    if serves.get("kind") != "agent":
        return None
    return str(serves.get("slug") or "")


def prime_dedicated_agents(runners) -> None:
    """Fill each of `runners`' `only_agent_slugs` cache — one query for them all,
    so the claim and the fleet list ask every candidate without a query each."""
    runners = [r for r in runners if r is not None and r.pk is not None]
    rows = (Runner.only_agents.through.objects.filter(runner_id__in=[r.pk for r in runners])
            .values_list("runner_id", "agent__slug")) if runners else []
    slugs: dict = {}
    for runner_id, slug in rows:
        slugs.setdefault(runner_id, set()).add(slug)
    for r in runners:
        setattr(r, Runner.ONLY_AGENTS_CACHE, slugs.get(r.pk, set()))


def dedicated_agent_slugs(runner: Runner) -> set[str] | None:
    """The agents a person dedicated `runner` to (`Runner.only_agents`); None when
    it is dedicated to none, which restricts nothing."""
    return runner.only_agent_slugs() or None


def _dedicated_refusal(runner: Runner, only: set[str]) -> str:
    return f"{runner.name} runs only {', '.join(sorted(only))}"


def prime_machine_runners(runners) -> None:
    """Cache, on each of `runners`, which agent each OTHER live runner on its
    machine runs alone — one query for them all, so the claim and the coverage report
    ask `serves_refusal` of every candidate without a query each. Primes their
    dedicated agents too (`prime_dedicated_agents`), which `serves_refusal` also asks."""
    runners = [r for r in runners if r is not None]
    prime_dedicated_agents(runners)
    machines = {r.machine_id for r in runners if r.machine_id}
    rows = list(Runner.objects.filter(machine_id__in=machines).exclude(status=Runner.RETIRED)
                .values_list("pk", "name", "machine_id", "capabilities")) if machines else []
    for r in runners:
        agent_runners: dict = {}
        for pk, name, machine, caps in rows:
            if machine != r.machine_id or pk == r.pk:
                continue
            slug = _served_agent_slug(serves_of(caps))
            if slug:
                agent_runners.setdefault(slug, name)
        setattr(r, _MACHINE_AGENT_RUNNERS, agent_runners)


def machine_agent_runners(runner: Runner) -> dict:
    """{agent slug: runner name} for the runners on `runner`'s machine that run one
    agent, other than itself — {} for a runner on no known machine."""
    if not runner.machine_id:
        return {}
    if not hasattr(runner, _MACHINE_AGENT_RUNNERS):
        prime_machine_runners([runner])
    return getattr(runner, _MACHINE_AGENT_RUNNERS)


def serves_refusal(runner: Runner, slug: str) -> str:
    """Why what `runner` serves on its machine keeps it from running agent `slug` —
    "" when it does not:

      - it is ONE AGENT's runner, and `slug` is not its agent;
      - a person dedicated it to other agents (`dedicated_agent_slugs`);
      - another live runner on its machine is the runner for agent `slug`, so the
        agent's secrets stay with that runner.

    Unlike the rest of `cannot_serve`, a pin does not get past this: the claim
    asks it above the pin, and the vault through `runner_routes_agent`."""
    if not slug:
        return ""
    own = _served_agent_slug(runner.serves())
    if own is not None and slug != own:
        return f"{runner.name} is the runner for agent {own or '(no agent named)'}"
    only = dedicated_agent_slugs(runner)
    if only is not None and slug not in only:
        return _dedicated_refusal(runner, only)
    sibling = machine_agent_runners(runner).get(slug)
    if sibling and own != slug:
        return f"{slug} has its own runner on this machine ({sibling})"
    return ""


def turn_serves_refusal(runner: Runner, turn) -> str:
    """`serves_refusal` for a whole turn: an agent's runner refuses every turn that does
    not run AS its agent — another agent's, and one with no acting agent at all
    (a repo turn, an agentless chat) — and any runner refuses a turn whose agent
    has its own runner elsewhere on its machine. A runner a person dedicated to
    some agents refuses the same way. "" when it takes the turn."""
    agent = turn.acting_agent
    own = _served_agent_slug(runner.serves())
    if own is not None and (agent is None or agent.slug != own):
        return f"{runner.name} is the runner for agent {own or '(no agent named)'}"
    only = dedicated_agent_slugs(runner)
    if only is not None and agent is None:
        return _dedicated_refusal(runner, only)
    return serves_refusal(runner, agent.slug) if agent is not None else ""
