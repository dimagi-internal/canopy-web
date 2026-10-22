"""One answer to "who does this bearer token speak for?".

Three doors read a raw bearer token — REST (`apps/tokens/middleware.py`), MCP
(`apps/mcp/auth.py`) and the WebSocket handshake (`apps/realtime/channels_auth.py`).
Each used to look the token up in its own order, with its own idea of which
token types it accepts and whether a use is stamped. They ask `resolve_bearer`
now, each naming the kinds it takes; what a kind may then REACH stays with the
door (a runner key opens the runner protocol only, an HCP token `/api/hcp/v1/`
only, a delegated token `site ∩ user`).

The kinds, tried in this order:

* ``runner-key`` — a runner's own `RunnerAccessToken` (by its prefix).
* ``hcp-oauth`` — an HCP service access token (by its prefix); no canopy user.
* ``pat`` — a `PersonalToken`, hand-minted or an OAuth access token; a use is
  stamped (`last_used_at`, `last_used_client`) when the door names its client.
* ``contact`` — a `ContactToken`; no canopy user, ever.
* ``delegated`` — a site's `DelegatedToken` for an active user.

Token formats are unchanged: only the two prefixed kinds are told apart by
their prefix; the rest are found by lookup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RUNNER_KEY, HCP_OAUTH, PAT, CONTACT, DELEGATED = (
    "runner-key", "hcp-oauth", "pat", "contact", "delegated")
ALL_KINDS = frozenset({RUNNER_KEY, HCP_OAUTH, PAT, CONTACT, DELEGATED})


@dataclass(frozen=True)
class Principal:
    kind: str
    #: The token row that matched.
    token: Any
    #: The canopy user it signs in as; None for a contact or an HCP client.
    user: Any = None
    #: `request.auth_method`: how sure we are of who this is.
    auth_method: str = ""
    #: `request.auth_credential`: which credential, never a secret.
    credential: dict = field(default_factory=dict)


def credential(kind: str, pk, label: str) -> dict:
    return {"type": kind, "id": pk, "label": (label or "")[:200]}


def resolve_bearer(raw: str, *, accept=ALL_KINDS, client: str | None = None) -> Principal | None:
    """The principal `raw` names, among the kinds `accept`, or None.

    Only the accepted kinds are looked up, so a door that takes one kind pays
    for one query. `client` (from `models.client_label`) stamps a PAT's use;
    None stamps nothing — for a re-check, or a request a session already
    signed in."""
    raw = (raw or "").strip()
    if not raw:
        return None
    from apps.contacts import hcp_oauth
    from apps.harness.models import RunnerAccessToken

    if raw.startswith(RunnerAccessToken.PREFIX):
        if RUNNER_KEY not in accept:
            return None
        rtok = RunnerAccessToken.lookup(raw)
        if rtok is None:
            return None
        return Principal(RUNNER_KEY, rtok, user=rtok.runner.owner, auth_method=RUNNER_KEY,
                         credential=credential(RUNNER_KEY, rtok.pk, rtok.runner.name))
    if raw.startswith(hcp_oauth.ACCESS_PREFIX):
        if HCP_OAUTH not in accept:
            return None
        tok = hcp_oauth.authenticate_access(raw)
        if tok is None:
            return None
        return Principal(HCP_OAUTH, tok, auth_method=HCP_OAUTH,
                         credential=credential(HCP_OAUTH, tok.pk, tok.client.name))

    from .models import ContactToken, DelegatedToken, PersonalToken, note_used

    if PAT in accept:
        pat = PersonalToken.lookup(raw)
        if pat is not None:
            if client is not None:
                note_used(pat, client)
            return Principal(PAT, pat, user=pat.user, auth_method=PAT,
                             credential=credential("oauth" if pat.oauth_grant_id else "pat",
                                                   pat.pk, pat.label))
    if CONTACT in accept:
        ctok = ContactToken.lookup(raw)
        if ctok is not None:
            return Principal(CONTACT, ctok, auth_method=CONTACT,
                             credential=credential(CONTACT, ctok.pk, ctok.app.name))
    if DELEGATED in accept:
        dtok = DelegatedToken.lookup(raw)
        if dtok is not None and dtok.user.is_active:
            return Principal(DELEGATED, dtok, user=dtok.user,
                             auth_method=dtok.assurance or DELEGATED,
                             credential=credential(DELEGATED, dtok.pk, dtok.app.name))
    return None
