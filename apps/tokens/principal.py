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

Every kind is told apart by its prefix: `crt_`, `hcpat_`, and — for tokens
minted since they were introduced — `cpat_`, `cdt_`, `ccon_` (the models'
`PREFIX`). A prefixed token is looked up in its own table only, and refused
when that kind is not one the door accepts. A token minted before the prefixes
is 43 bare base64url characters (`LEGACY_LENGTH`): it is tried against each
accepted kind in the order above, as before. Length is what tells a legacy
token that happens to begin `cpat_` from a prefixed one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .kinds import CONTACT, DELEGATED, HCP_OAUTH, OAUTH, PAT, RUNNER_KEY

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

    typed = _prefixed_kind(raw)
    if typed is not None:
        # One kind, one lookup; any other kind's table cannot hold this hash.
        if typed not in accept:
            return None
        accept = {typed}

    if PAT in accept:
        pat = PersonalToken.lookup(raw)
        if pat is not None:
            if client is not None:
                note_used(pat, client)
            return Principal(PAT, pat, user=pat.user, auth_method=PAT,
                             credential=credential(OAUTH if pat.oauth_grant_id else PAT,
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


#: `secrets.token_urlsafe(32)`: what every personal, delegated and contact token
#: was before the prefixes, and the random part of one after.
LEGACY_LENGTH = 43


def _prefixed_kind(raw: str) -> str | None:
    """The kind a prefixed token names, or None for a legacy (bare) token."""
    if len(raw) <= LEGACY_LENGTH:
        return None
    from .models import ContactToken, DelegatedToken, PersonalToken

    for kind, model in ((PAT, PersonalToken), (DELEGATED, DelegatedToken), (CONTACT, ContactToken)):
        if raw.startswith(model.PREFIX):
            return kind
    return None
