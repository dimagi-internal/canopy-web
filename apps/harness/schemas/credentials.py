"""Runner credential, login, mint and sign-in schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from ninja import Schema
from pydantic import ConfigDict, Field

from .. import clis as cli_vocab
from .credential_sets import RunnerCredentialSetOut

# ---- Runner credentials (per-runner, cloud-only; laptop uses emdash) ----
CredentialKind = Literal["login", "token", "api_key"]


class RunnerVaultEntryIn(Schema):
    """A token or API key whose value goes into the runner's vault: the entry is made
    here with no value, and the caller seals the value under `secret_name`."""

    kind: Literal["token", "api_key"]
    name: str = Field(default="", max_length=200)
    #: Which agent CLI it is for: a Claude token or API key, or an OpenAI API key for
    #: Codex (Codex has no setup-token).
    cli: cli_vocab.CliName = "claude-code"


class RunnerVaultEntryOut(Schema):
    key: str
    #: The name to seal the value under, in `vault_id`, at key version `kv`.
    secret_name: str
    vault_id: str
    kv: int


class RunnerVaultValueIn(Schema):
    name: str = Field(max_length=120)
    value_zk: str = Field(max_length=20000)


class RunnerVaultValuesIn(Schema):
    """The box's own write: `zk1:` ciphertext for names its credential list uses."""

    vault_id: str
    kv: int
    values: list[RunnerVaultValueIn] = Field(max_length=50)


class RunnerVaultValuesOut(Schema):
    names: list[str] = []


class RunnerCredentialPatchIn(Schema):
    """A new name for one token or API key. None leaves it unchanged. Anything else —
    a `value` above all — is refused: a value is sealed into the vault, never sent here."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)


class RunnerCascadeItem(Schema):
    """One credential the box uses, in order. A token's or API key's value is in a
    vault serving the runner (`vault_secret`); a login's lives on the box."""

    key: str
    kind: CredentialKind
    #: The agent CLI it signs in (`harness.clis`): the box keeps one cascade per CLI.
    cli: str = "claude-code"
    #: The name its value has in a vault serving this runner, when one holds it: the
    #: box reads it from there (`…/credential/resolve`, with its device key) and uses
    #: `value` only when it cannot.
    vault_secret: str = ""


class RunnerLoginRef(Schema):
    """One full login the box should keep, in cascade order."""

    key: str
    account: str = ""
    cli: str = "claude-code"


class RunnerCredentialOut(Schema):
    """The runner's own credential list — no value: a token's or API key's lives in a
    vault serving the runner, which the box opens with its device key."""

    #: The full logins this box should keep, first to last. Any login on the
    #: box that is not listed here is one a person removed.
    logins: list[RunnerLoginRef] = []
    #: Every credential, in the order the box uses them.
    cascade: list[RunnerCascadeItem] = []
    #: The runner has a vault with a key: a token the box signs in is sealed there
    #: rather than sent here (`RunnerMintResultIn.vault_key`).
    vault_open: bool = False
    updated_at: dt.datetime | None = None


class RunnerLoginOut(Schema):
    """A full login on the box, as the box last reported it."""

    key: str
    #: The account the box says this login belongs to; "" until it has said.
    account: str = ""
    signed_in: bool = False
    #: The agent CLI it signs in (`harness.clis`).
    cli: str = "claude-code"
    reported_at: dt.datetime | None = None


class RunnerCredentialEntryOut(Schema):
    """One credential in the list, masked."""

    key: str
    kind: CredentialKind
    #: The agent CLI it signs in (`harness.clis`).
    cli: str = "claude-code"
    #: A token's or API key's name, as a person gave it.
    name: str = ""
    #: A login's account, as the box reported it.
    account: str = ""
    #: A login: whether the box says it is signed in.
    signed_in: bool = False
    #: A token or API key: whether a value is held (never the value).
    has_value: bool = False
    #: A token or API key: whether a vault serving this runner holds its value.
    in_vault: bool = False
    #: A token or API key: the name its value has (or would have) in that vault.
    vault_secret: str = ""
    reported_at: dt.datetime | None = None


class RunnerLoginOrderIn(Schema):
    #: Every credential key on the runner, first to last.
    keys: list[str]


class RunnerLoginReportItem(Schema):
    key: str
    signed_in: bool = False
    account: str = Field(default="", max_length=254)


class RunnerLoginReportIn(Schema):
    logins: list[RunnerLoginReportItem] = []
    #: The CLI's own directory holds a login this list has never had.
    adopt_default: bool = False


class DroppedRouteOut(Schema):
    agent: str
    # "" = the agent's default runner order; otherwise the source a rule is for.
    source: str
    # "" = the rule applied to anyone; otherwise the person it routed.
    actor: str


class RetireOut(Schema):
    """What retiring took with it. A retired runner cannot stay routed, so its
    routing rows are deleted — and said so here, because silently dropping a
    person's route sent their work to someone else's box with nothing surfacing it."""
    runner: str
    dropped_routes: list[DroppedRouteOut] = []


class RunnerAdminOut(Schema):
    """One explicit grant. No secret here — who, by whom, when."""

    user_id: int
    email: str
    granted_by_email: str = ""
    created_at: dt.datetime


class RunnerAdminIn(Schema):
    email: str


class RunnerEnrollmentOut(Schema):
    """One workspace this runner has opted in to serving (`RunnerEnrollment`).

    `serving` is the live answer: enrolled AND the runner's owner may still run
    work there (editor and above). An enrollment whose owner was demoted reads
    `serving: false` — kept, so a re-promotion needs no re-enrolling, but it
    routes nothing meanwhile.

    An enrollment also covers every workspace BELOW it in the tree, including
    ones created later. `covers` names those the box serves through this row
    (the owner is an editor or above there) and the reader may see."""

    workspace: str
    display_name: str = ""
    serving: bool
    covers: list[str] = []
    enrolled_by_email: str = ""
    created_at: dt.datetime


class RunnerEnrollmentIn(Schema):
    workspace: str


class RunnerMintOut(Schema):
    """A browser-driven re-authentication, as an OPERATOR sees it.

    Deliberately carries no `code`: the authorization code is the human's to type
    once, and echoing it back to a screen serves nothing and widens where it can
    leak. The minted token never appears in any operator-facing shape at all.
    """

    id: uuid.UUID
    status: str
    #: Which subscription login the new token will replace — or `box`, a full
    #: login the runner keeps on the box (carries claude.ai connectors).
    slot: Literal["primary", "secondary", "box", "token"] = "primary"
    #: For a `box` sign-in, the login it signs in ("" = the box's default login);
    #: for a `token` one, the token it replaces ("" = a new token).
    login_key: str = ""
    authorize_url: str
    detail: str
    created_at: dt.datetime
    updated_at: dt.datetime


class RunnerMintStartIn(Schema):
    # `box` = the full sign-in kept on the runner; `token` = a long-lived token
    # (setup-token) for the list; `primary`/`secondary` = the old fixed slots.
    slot: Literal["primary", "secondary", "box", "token"] = "box"
    #: Which login to sign in again ("" = the box's default login), or which
    #: token to replace ("" = a new one).
    login_key: str = ""


class GoogleMintWorkOut(Schema):
    """A Google sign-in for an agent this runner holds, at the step the runner owes
    (`apps/agents/credentials/google_mint.py`). `requested`: make a PKCE verifier, keep
    it, post its challenge. `completing`: exchange `code` (consumed by this read) with
    the verifier and the OAuth client from the agent's vault, encrypt the token for the
    agent (under its vault key at `kv`, else sealed to `recipients`) and post only
    ciphertext. canopy-web never receives the token."""

    id: str
    agent: str
    status: Literal["requested", "completing"]
    code: str = ""
    redirect_uri: str = ""
    client_id: str = ""
    #: gog's name for the OAuth client the token is bound to.
    client: str = ""
    expected_account: str = ""
    agent_id: str = ""
    kv: int = 0
    #: `{kind, target, public_jwk}` — who may read the agent, for a box without its vault key.
    #: For a delegated sign-in: the machines that may hold the person's credential and
    #: their own vault key — the only readers it may be sealed to.
    recipients: list[dict] = []
    #: A person's OWN account, delegated to the agent: seal to `recipients` only (never
    #: under the vault key, even when this box holds it), under `name`, and post `name`.
    delegated: bool = False
    #: The vault entry the token is sealed under ("" = the agent's `gog-token`).
    name: str = ""
    #: A SESSION (a person lending their account from a page for up to an hour): seal the
    #: access token and its end, never a refresh token (Google issued none).
    session: bool = False
    session_minutes: int = 0


class RunnerMintClaimOut(Schema):
    """What the RUNNER polls for: the work it owes, and the code when there is one.

    Null `mint` is the ordinary answer — no sign-in is in progress — so this is
    cheap to poll on the tick the runner already runs. `google` is an agent's Google
    sign-in this runner owes a step on (a runner that declares `google_mint`)."""

    mint: RunnerMintOut | None = None
    code: str = ""
    google: GoogleMintWorkOut | None = None


class GoogleMintChallengeIn(Schema):
    #: base64url(SHA-256(verifier)) — the verifier never leaves the box.
    code_challenge: str = Field(max_length=128)


class GoogleMintResultIn(Schema):
    """How the runner's half ended: the token encrypted for the agent (`value_zk`, `zk1`
    or `zks1`, at `kv`) and the account that consented — or `error` (an outcome the
    settings page explains) with `detail`. Never a token in the clear."""

    value_zk: str = ""
    kv: int = 0
    account: str = Field(default="", max_length=254)
    #: The entry name the value was sealed under (the work's `name`; a delegated sign-in
    #: is refused without it, since a value sealed under another name opens nowhere).
    name: str = Field(default="", max_length=120)
    error: str = Field(default="", max_length=32)
    detail: str = Field(default="", max_length=900)


class RunnerMintUrlIn(Schema):
    url: str


class RunnerMintCodeIn(Schema):
    code: str


class RunnerMintEntryIn(Schema):
    vault_key: str = Field(max_length=8)


class RunnerMintResultIn(Schema):
    # A token is never accepted: one sent here (a box from before the vault) fails the
    # sign-in, unstored. `detail` says why an attempt failed, to the human waiting.
    token: str = ""
    detail: str = ""
    # A `box` sign-in delivers no token — the login stays on the runner — so
    # success is this flag instead.
    logged_in: bool = False
    #: The account a `box` sign-in signed in as, read back off the box.
    account: str = Field(default="", max_length=254)
    #: A `token` sign-in the box sealed into the runner's vault: the entry it filled
    #: (named first with `…/mint/entry`). Done only if the vault holds it; with a
    #: `detail`, the seal failed and an entry made for this sign-in goes again.
    vault_key: str = Field(default="", max_length=8)


class SignInRequestIn(Schema):
    provider: str = "aws"
    # The device-authorization URL the CLI on the box printed, code included.
    url: str
    # What is being signed in to, e.g. the AWS profile name ("labs").
    label: str = ""
    # Who is waiting on it, e.g. the agent ("hal"). Shown in the notification.
    requested_by: str = ""
    # Why the sign-in is needed. Required: a 422 without it.
    reason: str = ""


class SignInRequestOut(Schema):
    # Devices the notification reached. 0 = nobody saw it; fall back to another channel.
    sent: int
    # The stored request. The box sends it back with the outcome (empty for a
    # runner with no owner, where nothing is stored).
    id: str = ""


class SignInResultIn(Schema):
    # How the sign-in ended: "approved", "failed" or "expired".
    outcome: str


class SignInResultOut(Schema):
    id: str
    status: str


class OpenSignInRequestOut(Schema):
    """A sign-in still waiting on you, for the supervisor feed. `url` opens the
    AWS approval page with the code filled in, as the push does."""

    id: str
    runner_id: str
    runner_name: str
    provider: str
    label: str
    requested_by: str
    reason: str
    url: str
    sent: int
    created_at: dt.datetime
    expires_at: dt.datetime


class RunnerVaultLayerOut(Schema):
    """One vault serving a runner, in the shape an agent's resolve gives its workspace
    vault layers (`ResolveVaultLayerOut`), so `canopy` opens both with one function."""

    vault_id: str
    name: str
    workspace: str
    binding_id: str
    kv: int
    wrap: dict[str, Any] | None = None
    values: dict[str, str] = Field(default_factory=dict)


class RunnerVaultOut(Schema):
    """A vault that holds a runner's Claude credentials."""

    vault_id: str
    name: str
    #: Its key version; 0 = no key yet (someone has to make it on the vault's page).
    kv: int = 0


class RunnerCredentialResolveOut(Schema):
    """A runner's credentials, as ciphertext only its own device key opens: each vault
    serving the runner with this device's copy of its key (`wrap: null` when it holds
    none) and the values the runner's list names."""

    vaults: list[RunnerVaultLayerOut] = []


class RunnerCredentialStatusOut(Schema):
    """Masked view — booleans, never values. The POST response + any UI."""

    #: Every credential, in the order the box uses them.
    credentials: list[RunnerCredentialEntryOut] = []
    #: The fixed-slot view an older caller reads: the first two tokens, the first API key.
    has_claude_token: bool = False
    has_claude_token_secondary: bool = False
    has_claude_api_key: bool = False
    claude_token_label: str = ""
    claude_token_secondary_label: str = ""
    #: The full logins on the box, in the order it falls through them.
    logins: list[RunnerLoginOut] = []
    #: The vaults that hold this runner's tokens and API keys, oldest first.
    vaults: list[RunnerVaultOut] = []
    #: The credential set whose list this is, when the runner reads one.
    credential_set: RunnerCredentialSetOut | None = None
    updated_at: dt.datetime | None = None


class TurnAgentTokenOut(Schema):
    """An agent login for ONE turn this runner holds: a token for the turn's
    `Agent.user`, revoked when the turn stops executing here, expiring at
    `expires_at` at the latest. For that turn's environment only."""

    token: str
    expires_at: dt.datetime
    #: The agent it signs in as — a client checks it is the agent it runs.
    agent_slug: str
    #: Set when the login is SESSION-scoped (a laptop turn whose session outlives it):
    #: it is held while this session stays live on the runner, not while the turn runs.
    session_id: str = ""
    turn_id: str


class TurnGitHubTokenOut(Schema):
    """One turn's GitHub identity, and where its token is. A token a person delegated to
    the agent is in the agent's vault, sealed to the machines that run it: `token` is ""
    and `delegated` names the vault entry the box opens from its own resolve of `agent`.
    (`token` is always "" now; older runners still read it.)"""

    token: str = ""
    #: The agent vault entry holding the token (`github-token`), when it is there.
    delegated: str = ""
    #: The agent whose vault holds it: the turn's own, or its repo's (`RepoIdentity`).
    agent: str = ""
    expires_at: dt.datetime | None = None
    github_login: str = ""
    git_name: str = ""
    git_email: str = ""
    #: `owner/repo` of the agent's own repo.
    repo: str = ""
    #: `Name <email>` of whoever caused the turn, for a `Requested-by:` trailer;
    #: "" when nobody in particular did (a schedule, a drill).
    requested_by: str = ""


class RunnerGitHubReadinessOut(Schema):
    """Can one agent this runner serves open a pull request, checked against
    GitHub at the moment of asking — so a missing or expired grant surfaces when
    the box boots, not in the middle of a turn."""

    agent_slug: str
    #: ok | warn (works, but expires within two weeks) | fail
    status: str
    detail: str = ""
    login: str = ""
    expires_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Readiness drills
# ---------------------------------------------------------------------------


# Declared before the set schemas it names.
RunnerCredentialStatusOut.model_rebuild()
