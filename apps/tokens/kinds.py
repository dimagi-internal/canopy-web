"""Every kind of credential a request can arrive with, and what each may do — one table.

A request is authenticated by exactly one credential, and every door records which
(`request.auth_credential["type"]`, from `BearerTokenAuthMiddleware`, the MCP server
and the WebSocket handshake; a browser session records none and reads as `session`).
The questions asked of it — may it mint another credential, may it say it acts on
someone's behalf, is it a turn speaking for the person who asked — used to be string
comparisons written where each was needed, so a kind added later was whatever each
of those comparisons happened to make of it. They read this table now, and
`tests/test_token_kinds.py` fails a comparison against a kind's name written
anywhere else, and a kind a door can record that is not here.

What a kind may REACH (a runner key the runner protocol, an HCP token
`/api/hcp/v1/`, a delegated token `site ∩ user`, a caller token the MCP tool routes)
is enforced at the door, before any view; `reaches` here says where.
"""
from __future__ import annotations

from dataclasses import dataclass

SESSION, PAT, OAUTH, CALLER_TOKEN, DELEGATED, CONTACT, RUNNER_KEY, HCP_OAUTH, ACT_AS = (
    "session", "pat", "oauth", "caller_token", "delegated", "contact", "runner-key", "hcp-oauth",
    "act_as")


@dataclass(frozen=True)
class Kind:
    name: str
    #: What the credential is.
    what: str
    #: What it opens, and where that is enforced.
    reaches: str
    #: It signs the request in as a canopy user (`request.user`); False for a
    #: principal with no canopy account.
    signs_in_a_user: bool
    #: It may mint another credential (a PAT, a session, an agent's login). Only a
    #: credential at least as long-lived and as wide as what it mints: anything
    #: narrower would launder its limit away (`tokens.middleware.minting_refusal`).
    may_mint: bool = False
    #: It is a turn speaking for the person who asked, not that person: refused
    #: where only the person may act (their memory page, its audit and grants).
    speaks_for_a_turn: bool = False
    #: It may carry `X-Canopy-On-Behalf-Of` — only the login canopy-web issued
    #: for one turn, which is a PAT (`harness.turn_asker`).
    may_act_on_behalf: bool = False


KINDS: dict[str, Kind] = {k.name: k for k in (
    Kind(SESSION, "a person signed in through canopy's browser session",
         "everything the person may reach; secret writes behind a passkey step-up",
         signs_in_a_user=True, may_mint=True),
    Kind(PAT, "a personal access token, minted by hand or for an agent's or a turn's login",
         "everything its user may reach (`apps/tokens/models.PersonalToken`)",
         signs_in_a_user=True, may_mint=True, may_act_on_behalf=True),
    Kind(OAUTH, "an OAuth access token (a PersonalToken minted for a grant): an hour, dies with the grant",
         "everything its user may reach (`apps/oauth_server`)",
         signs_in_a_user=True),
    Kind(CALLER_TOKEN, "a conversation's caller token (`cct_`): lives as long as its asker",
         "MCP; over REST only the routes MCP serves as tools, and only the full-profile kind "
         "(`tokens.middleware._authenticate_caller_token`)",
         signs_in_a_user=True, speaks_for_a_turn=True),
    Kind(ACT_AS, "an act-as-you token (`cact_`): an agent acting as the person who started its "
         "turn, after they approved it with their passkey; 30 minutes at most, dies with the turn",
         "the routes MCP serves as tools, less `harness.act_as.EXCLUDED_*`; GET only under a "
         "read grant (`tokens.middleware._authenticate_act_as_token`, `mcp.act_as_scope`)",
         signs_in_a_user=True, speaks_for_a_turn=True),
    Kind(DELEGATED, "a site's delegated token for its visitor, or a person's MCP host grant",
         "`site ∩ user` (`apps/embedding/delegation.reaches`; `apps/mcp/delegation.py`)",
         signs_in_a_user=True),
    Kind(CONTACT, "a contact's token: somebody with no canopy account",
         "`/api/contact/` only (`apps/common/middleware.py`); never a `request.user`",
         signs_in_a_user=False),
    Kind(RUNNER_KEY, "a runner's own token, bought with its device key",
         "the runner protocol only (`apps/harness/runner_auth.reaches`)",
         signs_in_a_user=True),
    Kind(HCP_OAUTH, "an HCP service access token: an app canopy does not operate, under one person's grant",
         "`/api/hcp/v1/` only, within that grant", signs_in_a_user=False),
)}


def kind_of(credential: dict | None) -> Kind | None:
    """The kind of a recorded credential (`request.auth_credential`), `session` for
    none, None for a type this table does not know — which no check treats as allowed."""
    return KINDS.get((credential or {}).get("type") or SESSION)


def of_request(request) -> Kind | None:
    """The kind of credential that authenticated `request`."""
    return kind_of(getattr(request, "auth_credential", None))


def speaks_for_a_turn(request) -> bool:
    """A turn speaking for the person who asked, not the person (unknown counts as not
    the person)."""
    kind = of_request(request)
    return kind is None or kind.speaks_for_a_turn


def names(**having) -> frozenset[str]:
    """The kinds whose fields match, e.g. `names(may_mint=True)`."""
    return frozenset(k.name for k in KINDS.values()
                     if all(getattr(k, field) == value for field, value in having.items()))
