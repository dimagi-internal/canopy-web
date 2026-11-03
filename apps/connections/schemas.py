"""Schemas for connections (`/api/connections…`, an agent's connections, a workspace's
connection apps, a turn's providers).

Nothing here carries a token.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from apps.agents.schemas import ZkEndorsementOut
from apps.common.schemas import StrictModel


class ProviderOut(StrictModel):
    name: str = Field(description="The provider's stable key, e.g. `salesforce`: the `{provider}` in every "
                                  "connection URL.")
    title: str = Field(description="What to call it in the UI, e.g. `Salesforce`.")
    workspace: str = Field(
        default="",
        description="The workspace whose app this row connects with: the `{workspace}` in "
                    "`/auth/connections/{provider}/{workspace}/start/`. Empty on a person's row for a "
                    "provider no workspace of theirs has set up.")
    workspace_name: str = Field(default="", description="That workspace's display name.")
    configured: bool = Field(
        description="Whether the workspace has an app (client id and secret) for the provider. False "
                    "is a real state, not an error: the UI says 'not set up' instead of offering a button "
                    "that cannot work."
    )


class ScopeChoiceOut(StrictModel):
    scope: str = Field(description="The scope as the provider names it.")
    label: str = Field(description="What it allows, in words.")


class ConnectionStatementOut(StrictModel):
    version: int = Field(description="The owner's signed grant statement's version; 0 = never shared.")
    agents: list[str] = Field(default_factory=list,
                              description="Slugs of the agents the statement lends it to, sorted.")
    my_computers: list[str] = Field(
        default_factory=list,
        description="ECDH key thumbprints of the owner's own computers it may be used on.")
    endorsers: list[str] = Field(
        default_factory=list,
        description="Key thumbprints of the people, besides the owner, whose endorsement of a machine the "
                    "statement accepts.")


class WaitingOut(StrictModel):
    agent: str = Field(description="The slug of the agent the machine runs; empty for one of the owner's own "
                                   "computers.")
    runner: str = Field(description="The machine's runner name.")


class UnreachableOut(StrictModel):
    agent: str = Field(description="The slug of an agent the statement lends it to.")
    reason: Literal["unvouched", "no_machine", "lapsed"] = Field(
        description="Why no machine can receive it for that agent. `unvouched`: machines run it, and nobody the "
                    "statement names vouches for one yet (re-share, trusting who vouches). `no_machine`: nothing "
                    "that may hold it runs the agent. `lapsed`: this grant is no longer in force.")


class MyMachineOut(StrictModel):
    jkt: str = Field(description="The machine's ECDH key thumbprint, as `statement.my_computers` lists it.")
    name: str = Field(description="Its runner name.")
    fingerprint: str = Field(description="The thumbprint as the owner compares it: eight groups of four.")


class ConnectionOut(StrictModel):
    id: uuid.UUID
    provider: str
    title: str = Field(description="The provider's display name.")
    workspace: str = Field(description="The workspace whose app made this connection.")
    workspace_name: str = Field(description="That workspace's display name.")
    account_label: str = Field(description="Which account this is (e.g. a Salesforce username).")
    instance_url: str = Field(
        description="The account's own API base URL, for a provider that has one per account "
                    "(Salesforce); empty for a provider whose API host is fixed.")
    scopes: list[str] = Field(
        default_factory=list,
        description="What the grant allows at the provider (e.g. `api`, `refresh_token`).",
    )
    needs_reconnect: bool = Field(description="The stored grant can no longer mint a token; connect again.")
    state: Literal["unshared", "active", "revoking"] = Field(
        description="`unshared`: sealed to its owner only, so no agent or computer can use it yet (share it); "
                    "`active`: shared as the owner's signed statement allows; `revoking`: being disconnected.")
    statement: ConnectionStatementOut = Field(
        description="What the owner's signed grant statement allows (version 0 and empty when never shared).")
    holders: list[str] = Field(
        default_factory=list,
        description="Names of the runners whose machines hold it now, sorted. Empty while `unshared`.")
    waiting: list[WaitingOut] = Field(
        default_factory=list,
        description="Machines the statement allows that do not hold it yet: a machine that holds it gives them "
                    "a copy, or the owner does from Settings. Empty unless `active`.")
    unreachable: list[UnreachableOut] = Field(
        default_factory=list,
        description="Agents in the statement that no machine can receive it for, so lending to them has reached "
                    "nothing yet. Empty unless `active`.")
    my_machines: list[MyMachineOut] = Field(
        default_factory=list,
        description="The owner's own computers the statement lets it be used on (`Use on my computers`), "
                    "named. Empty for an agent's connection.")
    owner_key_replaced: bool = Field(
        default=False,
        description="It is sealed to a vault key whoever connected it has since replaced: new tokens stay readable "
                    "by the old key until it is disconnected and connected again. False on a row lent to an agent.")
    server_use: bool = Field(
        default=False, description="canopy-web may use this connection for screens and apps (the owner's switch).")
    server_reachable: bool | None = Field(
        default=False, description="canopy-web can open the stored sign-in now: the switch is on and the envelope "
                                    "is sealed to its key. False: it is sealed without canopy-web (a sign-in while "
                                    "the key service was failing; connect again to fix) or the switch is off. Null: "
                                    "the switch is on and the key can't be read right now, so nobody can say; "
                                    "nothing needs doing.")
    provider_calls: bool = Field(
        default=False, description="canopy-web can be used for this connection, so it has a \"Used by canopy-web\" "
                                   "switch: canopy-web makes calls for the provider (Salesforce; not Google), it "
                                   "is not lent with limits (`expires_at`, `owner_turns_only`), and canopy-web has "
                                   "a key set up or the switch is already on.")
    usable: bool = Field(
        description="Whether it can hand out a token now. A person's: only while they can still read "
                    "`workspace` (False once they have left it; disconnect it). An agent's own: only while the "
                    "agent is in `workspace` (False once it has moved; disconnect it and connect again in its "
                    "workspace). One lent to an agent: only while that grant is in force (the agent is in `workspace`, "
                    "the lender still administers it, and an owning agent has not moved).")
    missing_scopes: list[ScopeChoiceOut] = Field(
        default_factory=list,
        description="Scopes the app now asks for that this grant lacks (an admin chose them after it was "
                    "connected), with what each allows; reconnect to allow them. Empty when nothing is missing.")
    expires_at: datetime | None = Field(
        default=None,
        description="When a lend for a while ends: no machine is handed it from then on and it is disconnected "
                    "soon after, which revokes it. Null: until it is disconnected.")
    owner_turns_only: bool = Field(
        default=False, description="It is staged only on turns its owner started, never anyone else's on that agent.")
    connected_at: datetime
    last_used_at: datetime | None = None
    connected_by_email: str = Field(
        description="Who completed the sign-in; empty when that person's account no longer exists.")
    granted_from: str | None = Field(
        default=None,
        description="For an agent's list, when this connection is lent to it rather than its own: who lent "
                    "it, the owning agent's slug or the person's name.",
    )
    shared_with: list[str] = Field(
        default_factory=list,
        description="For an agent's own connection: slugs of the agents it is shared with, sorted. "
                    "Empty for a person's connection and for one shared with this agent.",
    )


class DisconnectOut(StrictModel):
    outcome: Literal["revoking", "revoked", "deleted"] = Field(
        description="`revoked`: canopy-web revoked it at the provider itself and the row is gone. `revoking`: it "
                    "will be revoked at the provider, then deleted: by a machine that holds it "
                    "(`machine_revokes`), or by canopy-web's own retries. `deleted`: canopy deleted its copy without "
                    "revoking it, because nothing could, or another connection of that account still needs the "
                    "sign-in (the provider may then keep it alive until it idles out).")
    machine_revokes: bool = Field(
        default=False, description="`revoking` only: a machine that holds it, heard from recently, can revoke it.")


class ServerUseIn(StrictModel):
    on: bool = Field(description="Only `false` is accepted here; turning canopy-web on goes through Share.")


class ConnectionsOut(StrictModel):
    providers: list[ProviderOut]
    connections: list[ConnectionOut]


class OptionFieldOut(StrictModel):
    name: str = Field(description="The key in `options`.")
    label: str
    kind: Literal["url", "int", "text"]
    required: bool
    default: str | int | None = None
    help: str = Field(default="", description="What to enter and how it is used, for the form to show.")


class ConnectionAppOut(StrictModel):
    provider: str = Field(description="The provider's key, e.g. `salesforce`.")
    title: str = Field(description="The provider's display name.")
    configured: bool = Field(description="A client id and secret are saved, so people and agents can connect.")
    client_id: str = Field(description="The OAuth client id (public); empty when nothing is saved.")
    has_secret: bool = Field(description="A client secret is saved and readable. The secret itself is never "
                                         "returned. False when the saved one can no longer be decrypted: "
                                         "enter it again.")
    options: dict[str, Any] = Field(default_factory=dict,
                                    description="The provider's own settings, as saved (see `option_fields`).")
    scopes: list[str] = Field(default_factory=list,
                              description="Extra scopes chosen from `scope_choices`, asked for on every connect.")
    scope_choices: list[ScopeChoiceOut] = Field(default_factory=list,
                                                description="Extra scopes this provider offers.")
    option_fields: list[OptionFieldOut] = Field(default_factory=list,
                                                description="The settings this provider takes.")
    callback_url: str = Field(description="Register this as the callback (redirect) URL of the OAuth client "
                                          "at the provider.")
    locked_options: list[str] = Field(
        default_factory=list,
        description="The `option_fields` that name the provider's authorization server (Salesforce: "
                    "`login_url`): like `client_id`, they cannot change while `connection_count` is above zero "
                    "(409 `app_locked`).")
    connection_count: int = Field(description="Connections made with this app; removing it disconnects them.")
    updated_at: datetime | None = None
    configured_by_email: str = Field(default="", description="Who last saved it; empty if unknown.")


class ConnectionAppsOut(StrictModel):
    apps: list[ConnectionAppOut] = Field(description="One row per provider, set up or not.")


class ConnectionAppIn(StrictModel):
    client_id: str = Field(max_length=255, description="The OAuth client id (consumer key) from the provider.")
    client_secret: str = Field(
        default="", max_length=4096,
        description="The client secret. Blank keeps the saved one (send a new one to rotate it); required "
                    "the first time and with a new client id.")
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="The provider's settings (`option_fields`), merged over the saved ones.")
    scopes: list[str] = Field(
        default_factory=list,
        description="The full list of extra scopes, from `scope_choices`; replaces the saved list.")


class TurnConnectionsOut(StrictModel):
    providers: list[str] = Field(
        description="Provider keys the turn's agent has a connection for (its own or one shared "
                    "with it), sorted. Empty for most agents.")


class ShareCandidateOut(StrictModel):
    kind: Literal["device", "member"]
    target: str = Field(description="The device's ECDH key thumbprint, or the owner's user id.")
    public_jwk: dict[str, Any] = Field(description="The key to seal to.")
    agent_id: str = Field(default="", description="The agent (pk) whose machine this is; empty for the owner "
                                                  "and their own computers.")
    runner_name: str = Field(default="", description="The machine's runner name; empty for the owner.")
    mine: bool = Field(description="The owner's own: their member key, or a machine they own.")


class ShareOwnerOut(StrictModel):
    user_id: str
    public_jwk: dict[str, Any] = Field(description="The owner's member public key, whose thumbprint is the "
                                                   "`owner_key` sealed in the envelope.")


class ShareAgentOut(StrictModel):
    id: str = Field(description="The agent's pk, as a statement lists it.")
    slug: str
    name: str


class ShareServerOut(StrictModel):
    available: bool = Field(
        description="canopy-web has a key it can be sealed to and could read it just now. False when none is set "
                    "up or the key service is failing; then `thumbprint` is empty and `public_jwk` null.")
    thumbprint: str = Field(description="The key's thumbprint, which a statement's `canopy_web` names; empty when "
                                        "not `available`.")
    public_jwk: dict[str, Any] | None = Field(
        default=None, description="The key (P-256 public JWK) to seal to; null when not `available`.")
    on: bool = Field(description="canopy-web may use this connection now (the switch on the row).")
    statements_enabled: bool = Field(
        description="This deployment accepts a statement naming canopy-web. When false, sharing with it is refused.")
    provider_calls: bool = Field(description="canopy-web makes calls for this provider and this connection is not "
                                             "lent with limits; the switch is shown only when true.")
    reachable: bool | None = Field(
        description="The stored envelope is sealed to canopy-web's key now, so it can use the connection. False "
                    "when the switch is off, or on but the key could not be used at sign-in. Null when the switch "
                    "is on and the key can't be read right now.")


class ShareStateOut(StrictModel):
    id: uuid.UUID
    binding: str = Field(description="`connection:<uuid>`, the binding id both envelopes are sealed to.")
    provider: str
    account_id: str
    account_label: str
    state: Literal["unshared", "active", "revoking"]
    sealed: str = Field(description="The refresh envelope (`zks1`); only its recipients can open it.")
    sealed_sig: dict[str, Any] | None = Field(
        default=None, description="Its writer MACs; null only on canopy-web's first seal, at connect.")
    sealed_digest: str = Field(description="SHA-256 of `sealed`: the `expect` of the next write.")
    access_sealed: str = Field(description="The access envelope; empty until a machine first refreshes.")
    access_sig: dict[str, Any] | None = None
    statement: dict[str, Any] | None = Field(
        default=None, description="The stored signed grant statement; null before the first share.")
    statement_sig: str = Field(
        default="", description="The owner's signature over `statement`; a device writer counts only under a "
                                "statement this verifies. Empty before the first share.")
    endorsers: list[str] = Field(
        default_factory=list,
        description="The stored statement's `endorsers`: key thumbprints of the people, besides the owner, whose "
                    "endorsement of a machine it accepts. Empty before the first share.")
    owner: ShareOwnerOut
    owner_agent: ShareAgentOut | None = Field(
        default=None, description="The agent whose connection this is, which the statement must list; null for a "
                                  "person's.")
    candidates: list[ShareCandidateOut] = Field(
        description="Everyone the owner could seal to: their member key, each machine of an agent they could "
                    "lend it to (one row per agent that admits it), and their own computers. A machine is sealed "
                    "to only when the statement allows it AND the browser trusts its endorsement.")
    endorsements: list[ZkEndorsementOut] = Field(
        default_factory=list, description="Endorsements of those machines' keys and of the device that last wrote "
                                          "each envelope, each with its `agent_id` and its signer's "
                                          "`by_public_jwk`, whose thumbprint is what `endorsers` lists.")
    agents: list[ShareAgentOut] = Field(description="The agents the owner may lend it to.")
    server: ShareServerOut = Field(description="canopy-web as a reader of this connection.")
    expires_at: datetime | None = Field(
        default=None, description="When this lend ends and it is disconnected; null when it has no time limit.")
    owner_turns_only: bool = Field(default=False, description="Agents use it only on turns its owner started.")


class ShareIn(StrictModel):
    statement: dict[str, Any] = Field(description="The grant statement (contract §0.3), signed.")
    sig: str = Field(max_length=128, description="The owner's ECDSA P-256 signature over it (P1363, base64url).")
    by_jkt: str = Field(max_length=64, description="The signer's member key thumbprint: the `owner_key`.")
    sealed: str = Field(description="The refresh envelope, re-sealed to the owner and the machines the "
                                    "statement allows.")
    sealed_sig: dict[str, Any] | None = Field(description="Its writer MACs, by the owner's member key.")
    access_sealed: str | None = Field(default=None, description="The access envelope re-sealed likewise, "
                                                                "when there is one.")
    access_sig: dict[str, Any] | None = None
    expect: str = Field(max_length=64, description="`sealed_digest` as read: the write replaces exactly that.")


# ---- a machine's material, lease and write-back (routes 7-12) ----------------------

class MaterialAppOut(StrictModel):
    client_id: str = Field(description="The app's OAuth client id; a machine refuses material whose client id "
                                       "differs from the one sealed in the envelope.")
    client_secret: str = Field(description="The app's client secret, to refresh with. Only ever in this "
                                           "response, to a machine that proved itself.")
    options: dict[str, Any] = Field(default_factory=dict,
                                    description="The app's settings; only `access_token_seconds` is read.")


class MaterialRecipientOut(StrictModel):
    kind: Literal["device", "member"]
    target: str = Field(description="A device's ECDH key thumbprint, or the owner's user id.")
    public_jwk: dict[str, Any]
    runner_name: str = Field(default="", description="The device's runner name; empty for the owner.")


class MaterialLeaseOut(StrictModel):
    mine: bool = Field(description="This machine holds the refresh lease.")
    ttl_seconds: int = Field(description="Seconds the lease has left, by canopy-web's clock.")


class ConnectionMaterialOut(StrictModel):
    id: uuid.UUID
    provider: str
    workspace: str = Field(description="The workspace whose app made the connection.")
    binding: str = Field(description="`connection:<uuid>`, the binding id both envelopes are sealed to.")
    account_id: str
    account_label: str
    sealed: str = Field(description="The refresh envelope (`zks1`); this machine opens it with its ECDH key.")
    sealed_sig: dict[str, Any] | None = Field(default=None, description="Its writer MACs.")
    sealed_digest: str = Field(description="SHA-256 of `sealed`: the `expect` of a write-back.")
    access_sealed: str = Field(description="The access envelope; empty until a machine first refreshes.")
    access_sig: dict[str, Any] | None = None
    access_expires_at: datetime | None = Field(
        default=None, description="When the sealed access token expires, so a machine can decide without "
                                  "opening it.")
    statement: dict[str, Any] | None = Field(default=None, description="The owner's signed grant statement.")
    statement_sig: str = Field(description="Its signature, by the owner's key.")
    owner: ShareOwnerOut
    endorsements: list[ZkEndorsementOut] = Field(
        default_factory=list,
        description="Endorsements of the recipient machines' keys and of the device that last wrote each envelope "
                    "(even one no longer a recipient), each with its `agent_id`.")
    recipients: list[MaterialRecipientOut] = Field(
        description="Who canopy-web lets the envelopes reach now. A machine seals only to those the signed "
                    "statement also allows: this list narrows, never widens.")
    app: MaterialAppOut
    lease: MaterialLeaseOut | None = Field(default=None,
                                           description="The live refresh lease, if any; null when none is held.")
    rotates_refresh_token: bool = Field(description="A refresh may hand back a new refresh token (write it back "
                                                    "as `sealed`).")
    agent_id: str = Field(default="", description="On a turn's read: the pk of the agent it is for, as the statement "
                                                  "lists it. A box refuses a statement that does not list it.")
    agent_slug: str = Field(default="", description="On a turn's read: that agent's slug. A box refuses material for "
                                                    "another agent than the one it is staging.")


class LeaseIn(StrictModel):
    lease_id: uuid.UUID | None = Field(default=None, description="The lease this machine holds, to renew it.")


class LeaseOut(StrictModel):
    lease_id: uuid.UUID
    ttl_seconds: int = Field(description="How long the lease lasts from now; renew before it runs out, on the "
                                         "machine's own monotonic clock.")
    expires_at: datetime


class WriteBackIn(StrictModel):
    lease_id: uuid.UUID
    sealed: str | None = Field(default=None, description="The refresh envelope, only when the refresh token "
                                                         "rotated.")
    sealed_sig: dict[str, Any] | None = None
    access_sealed: str = Field(description="The new access envelope, to the same recipients.")
    access_sig: dict[str, Any] | None = Field(description="Its writer MACs, by this machine's ECDH key. They bind "
                                                          "the SHA-256 (hex) of the stored `access_sealed` it "
                                                          "replaces (empty string when none), not `expect`.")
    access_expires_at: datetime
    expect: str = Field(max_length=64, description="`sealed_digest` as read: the refresh envelope's write replaces "
                                                   "exactly that. (The access envelope's writer MAC names the "
                                                   "digest of the stored `access_sealed` instead.)")
    rotated: bool = Field(description="The provider handed back a new refresh token.")


class WriteBackOut(StrictModel):
    sealed_digest: str = Field(description="The digest now stored: the `expect` of the next write.")


class ReconnectRequiredIn(StrictModel):
    lease_id: uuid.UUID
    error: str = Field(max_length=100, description="The provider's error code; only `invalid_grant` marks the "
                                                   "connection `needs_reconnect`.")
    detail: str = Field(default="", max_length=300,
                        description="The provider's description of the refusal, scrubbed of anything the machine "
                                    "sent it. Recorded with the error; never a token.")


class ConnectionKeeperWorkOut(ConnectionMaterialOut):
    waiting: list[str] = Field(default_factory=list, description="ECDH thumbprints of the machines the statement "
                                                                 "allows that hold no copy yet: re-seal for them.")
    revoking: bool = Field(description="Being disconnected: revoke it and confirm.")
    revoke_at_provider: bool = Field(description="Revoke at the provider too; false while another connection of "
                                                 "the same account through the same app remains.")


class ConnectionKeeperOut(StrictModel):
    connections: list[ConnectionKeeperWorkOut]


class ResealIn(StrictModel):
    sealed: str = Field(description="The same refresh plaintext, re-sealed to the owner and allowed machines.")
    sealed_sig: dict[str, Any] | None = Field(description="Its writer MACs, by this box's ECDH key.")
    access_sealed: str = Field(default="", description="The access envelope re-sealed likewise; empty only "
                                                       "when none is stored.")
    access_sig: dict[str, Any] | None = None
    expect: str = Field(max_length=64, description="`sealed_digest` as read.")


class ResealOut(StrictModel):
    machines: list[str] = Field(description="The machines it is now sealed to.")


class RevokedIn(StrictModel):
    revoked: bool = Field(description="Revoked at the provider (or told not to); false when that failed.")
    error: str = Field(default="", max_length=100, description="The provider's error code, when it failed.")


class ViewSurfaceIn(StrictModel):
    kind: Literal["view"]
    id: int = Field(description="The dynamic view.")
    version: int = Field(description="The revision the page drew. What that revision declares is what is checked, "
                                     "not the view's latest.")


class AppSurfaceIn(StrictModel):
    kind: Literal["app"]
    workspace: str = Field(description="The workspace the app is installed in.")
    key: str = Field(description="The installed app's key.")


class ConnectionCallIn(StrictModel):
    method: str = Field(description="`GET`, `POST`, `PATCH` or `DELETE`.")
    path: str = Field(description="Relative to Salesforce's API root, for example `query` or `sobjects/Lead/00Q…`. "
                                  "Plain names and slashes only.")
    query: dict[str, str | list[str]] | None = Field(default=None, description="The query string, as names and text.")
    body: Any = Field(default=None, description="The JSON body to send, on a `POST` or `PATCH`.")
    surface: Annotated[ViewSurfaceIn | AppSurfaceIn, Field(discriminator="kind")] = Field(
        description="The screen making the call: its own workspace's connection is the one used.")
    as_: Literal["viewer", "shared"] = Field(
        default="viewer", alias="as", description="Whose connection: the viewer's own, or one an agent shared.")
    agent: str | None = Field(default=None, description="With `as: shared`, the agent whose connection it is.")
    operation_id: str | None = Field(default=None, max_length=100, description="The click a write belongs to.")


class ConnectionCallOut(StrictModel):
    status: int = Field(description="The provider's own status, also when it refused the call.")
    body: Any = Field(default=None, description="The provider's JSON answer. Null when it sent none, or not JSON.")
    text: str | None = Field(default=None, description="The start of an answer that was not JSON. Absent otherwise.")
