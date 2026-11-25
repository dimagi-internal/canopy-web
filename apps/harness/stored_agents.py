"""Stored agents, as the runner side sees them: an agent whose definition is a git
repository canopy-web hosts (`Agent.source == "stored"`) rather than a GitHub repo.

A runner brings the live revision into a local clone (`canopy agent prepare --runner` /
`canopy agent sync --runner`, which fetch canopy's repository with the box's own token),
so everything downstream of "a clone at `AGENT_ROOT/<slug>`" is unchanged. What the server tells the runner, and what routing
must not refuse such an agent for, is decided here, once:

- the claim carries `definition` (`ClaimedTurnOut.definition`): which kind the turn's agent
  is, and for a stored agent its live revision — the runner syncs instead of `git pull`.
  A repo agent's says `source: "repo"` too, so a box whose clone of it is still a
  stored agent's (an agent exported to GitHub since) clones it again from GitHub;
- a stored agent's turn gets no GitHub identity (`api_runners_credentials.turn_github_token`);
- a laptop has no emdash project for a stored agent and needs none (`routing.cannot_serve`).

Read defensively: the model fields belong to the agents app. Rules: `docs/architecture/runners.md` § A stored agent
on a runner."""
from __future__ import annotations

STORED = "stored"
REPO = "repo"


def source_of(agent) -> str:
    """`"stored"` or `"repo"` — `"repo"` for no agent, and for a model without the field."""
    if agent is None:
        return REPO
    return STORED if getattr(agent, "source", REPO) == STORED else REPO


def is_stored(agent) -> bool:
    return source_of(agent) == STORED


def definition_of(agent) -> dict | None:
    """What a claim tells the runner about the turn's agent definition: for a stored agent
    `{source, revision, root_sha256}` (its live revision; None/"" before one is published);
    for a repo agent `{"source": "repo", "revision": None, "root_sha256": ""}`; None for a
    turn with no agent."""
    if agent is None:
        return None
    if not is_stored(agent):
        return {"source": REPO, "revision": None, "root_sha256": ""}
    rev = getattr(agent, "live_revision", None)
    return {
        "source": STORED,
        "revision": getattr(rev, "number", None) if rev is not None else None,
        "root_sha256": str(getattr(rev, "root_sha256", "") or "") if rev is not None else "",
    }
