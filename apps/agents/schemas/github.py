from __future__ import annotations

from datetime import datetime

from pydantic import Field

from apps.common.schemas import StrictModel


# ---- GitHub: a person's identity, lent to one agent (a delegated vault entry) ----
class AgentGitHubIn(StrictModel):
    token: str


class AgentGitHubCheckOut(StrictModel):
    repo: str
    ok: bool
    detail: str = ""


class AgentGitHubOut(StrictModel):
    """What the settings screen shows about the owner's GitHub delegation to
    this agent. Never the token."""

    #: `owner/repo`, parsed from the agent's repo URL; "" when it is not GitHub.
    repo: str = ""
    owner_email: str = ""
    #: Who delegated the token in force. "" when none is.
    lent_by_email: str = ""
    #: Whether the CALLER may lend this agent a token — its owner or an admin.
    can_lend: bool = False
    #: GitHub's new-token form, pre-filled for this agent.
    create_url: str = ""
    set: bool = False
    #: The GitHub account the token acts as, and the name commits carry.
    login: str = ""
    name: str = ""
    expires_at: datetime | None = None
    expired: bool = False
    expiring_soon: bool = False
    checks: list[AgentGitHubCheckOut] = Field(default_factory=list)
    #: Why the last check could not run at all (GitHub rejected the token).
    error: str = ""
    checked_at: datetime | None = None
    updated_at: datetime | None = None
    #: Repos whose agentless project turns (`Turn.project`, no agent) run as
    #: this agent's GitHub identity — its `RepoIdentity` rows. Read-only here;
    #: set in Django admin.
    identity_for_repos: list[str] = Field(default_factory=list)
    #: The token is a credential delegated to the agent, in its vault: sealed to the
    #: machines that run it and readable by no person. Always true now; kept for older clients.
    in_vault: bool = False
    #: "GitHub · delegated by Jonathan Jackson · GitHub says it expires 12 Jan 2027".
    account_label: str = ""
    #: The machines that can open it, and the ones that run the agent but wait for a
    #: machine that holds it to seal it to them.
    machines: list[str] = Field(default_factory=list)
    waiting_machines: list[str] = Field(default_factory=list)
    #: The machines a token YOU gave now would be sealed to: the cloud boxes that run the
    #: agent, and runners of your own that do.
    eligible_machines: list[str] = Field(default_factory=list)
    #: Who ran the last check: "server" (when it was given) or a machine's name.
    checked_by: str = ""
    #: A machine's probe failed: the token no longer works, and its person replaces it.
    no_longer_works: bool = False


# ---- A2A Agent Card (apps/agents/agent_card.py) ----
#
# The Agent2Agent (A2A) protocol v1.0 AgentCard, field for field from the
# normative `lf.a2a.v1` proto (specification/a2a.proto). A2A's JSON form is
# camelCase (spec §5.5), so each model generates camelCase aliases and the
# routes serialize `by_alias`; optional fields that are unset are OMITTED
# (`exclude_none`), which is also what §8.4.1's canonicalization requires the
# day these cards are signed. Output-only: nothing parses a card from a client.
