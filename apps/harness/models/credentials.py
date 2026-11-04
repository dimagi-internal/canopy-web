"""Runner credentials, logins, mints, sign-in requests and device keys.

Framework tier. Split from the former models.py; `apps.harness.models` re-exports.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.token_hash import hash_token

from .. import clis
from .runner import Runner


class RunnerCredential(models.Model):
    """Per-runner credentials the runner fetches at startup — CLOUD runners only.

    Laptop/emdash runners use their ambient auth (emdash already holds the Claude
    login) and never read this. A cloud runner boots knowing only its canopy-pat,
    then fetches this bundle over HTTPS authed by that PAT (owner == owner, the
    same gate as heartbeat/claim) and stages it into its environment: the runner's
    Claude login, and nothing else.

    NO GITHUB TOKEN. There was one — a shared fine-grained PAT, staged box-wide
    (`GH_TOKEN` in the runner's environment plus a global `~/.git-credentials`),
    so every agent on the box pushed as whoever made it, with whatever it could
    do; it could push branches and could not open pull requests (#747). GitHub is
    now a person's identity lent to that one agent (delegated in its vault),
    handed to one turn at a time.

    NO BOX-WIDE VAULT KEY. Every secret lives in a canopy vault (the agent's own,
    and workspace vaults layered under it), opened with this box's device key; the
    runner holds no credential that reads every agent's vault.

    The credentials themselves are `RunnerLogin` rows, one ordered list. This row
    is the bundle's stamp: `updated_at` moves on every change to that list, and a
    box that sees it move proves its credentials again.
    """

    runner = models.OneToOneField(Runner, on_delete=models.CASCADE, related_name="credential")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    def __str__(self) -> str:
        return f"credential:{self.runner_id}"


class RunnerLogin(models.Model):
    """One Claude credential of a cloud runner, in its place in ONE ordered list.

    Claude auth is an ordered cascade, not one credential: a subscription has a
    usage cap, and when it trips every agent on the box stops (2026-08-01). The
    box uses the first credential that works and moves down the list when one is
    capped or stops working. Any number of each kind, in any order a person sets:

    - `login`: a full `claude auth login` held ON the box. Its refresh token
      rotates, so it never lives here; it sits in its own config directory on the
      box (`~/.claude-logins/<key>`; `default` is the CLI's own `~/.claude`).
      `account` and `signed_in` are what the BOX reports, so nobody types a name
      that can disagree with the login it holds.
    - `token`: a `claude setup-token`; `api_key`: an Anthropic API key. Their VALUES
      are never here: each lives in a vault serving the runner, under
      `vault_secret_name`, sealed by the box after a sign-in or by a person's browser,
      and opened only by the box's own device key (docs/architecture/vault.md § A
      runner's own vault).

    A token or API key cannot say whose it is, so a person names it (`name`).
    Adding a login row starts a sign-in; deleting any row makes the box drop it
    on its next re-read; the order of the rows is the cascade order.
    """

    DEFAULT = "default"
    LOGIN = "login"
    TOKEN = "token"
    API_KEY = "api_key"
    KIND_CHOICES = [(LOGIN, "login"), (TOKEN, "token"), (API_KEY, "API key")]

    #: Whose list this entry is on: ONE runner's, or a credential set's (which every
    #: runner on the set reads as its own, `runners.list_owner`). Exactly one.
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="logins",
                               null=True, blank=True)
    credential_set = models.ForeignKey("RunnerCredentialSet", on_delete=models.CASCADE,
                                       related_name="entries", null=True, blank=True)
    #: Names the entry (and a login's directory on the box). Short, opaque, never reused.
    key = models.CharField(max_length=32)
    # db_default as well: code from before the list, still serving mid-deploy,
    # inserts login rows without these columns.
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=LOGIN, db_default=LOGIN)
    #: Which agent CLI this credential signs in (`clis.CLIS`). The box keeps one
    #: cascade per CLI, in this list's order.
    cli = models.CharField(max_length=32, choices=clis.CHOICES, default=clis.CLAUDE_CODE,
                           db_default=clis.CLAUDE_CODE)
    position = models.PositiveIntegerField(default=0)
    #: A token's or API key's name, as a person gave it. Not a secret.
    name = models.CharField(max_length=200, blank=True, default="", db_default="")
    account = models.CharField(max_length=254, blank=True, default="")
    signed_in = models.BooleanField(default=False)
    reported_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["runner", "key"], name="uniq_runner_login_key"),
            models.UniqueConstraint(fields=["credential_set", "key"], name="uniq_credential_set_entry_key"),
            models.CheckConstraint(
                condition=(models.Q(runner__isnull=False, credential_set__isnull=True)
                           | models.Q(runner__isnull=True, credential_set__isnull=False)),
                name="runner_login_one_list"),
        ]

    def __str__(self) -> str:
        return f"login:{self.runner_id or self.credential_set_id}:{self.key}"

    #: The vault secret that holds a token's or API key's value, by kind. Derived
    #: from `key`, so an entry and its secret can never name different things.
    VAULT_SECRET_PREFIX = {TOKEN: "claude-token:", API_KEY: "anthropic-api-key:"}

    @property
    def vault_secret_name(self) -> str:
        """The name its value has in a vault attached to the runner; "" for a login."""
        if self.cli == clis.CODEX and self.kind == self.API_KEY:
            return f"openai-api-key:{self.key}"
        prefix = self.VAULT_SECRET_PREFIX.get(self.kind)
        return f"{prefix}{self.key}" if prefix else ""


class RunnerCredentialRead(models.Model):
    """One read of one of a runner's Claude credentials out of its vault — the read
    log (`GET …/runners/{id}/credential/resolve`), one row per secret handed out, as
    `AgentCredentialRead` is for an agent's. `name` is a plain string: deleting the
    secret must not erase the record that it was read."""

    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="credential_reads")
    device_key = models.ForeignKey("RunnerDeviceKey", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    vault = models.ForeignKey("agents.Vault", on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+")
    name = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["runner", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.runner_id}:{self.name}@{self.created_at:%Y-%m-%d %H:%M}"


class RunnerMint(models.Model):
    """One browser-driven re-authentication of a runner's Claude credential.

    A cloud runner's subscription token expires often, and when it does the box
    does not slow down — it stops, and stays stopped until a person signs in
    again. Fixing that used to mean a terminal on the box.

    It cannot be done entirely in canopy-web: `claude setup-token` runs a PKCE
    flow whose verifier is generated by whoever starts it, so canopy-web cannot
    exchange a code a browser collected — it is not the OAuth client and must not
    pretend to be. So the RUNNER runs the real CLI, and this row is the relay
    between it and a human's browser: the runner posts the authorize URL, a
    person opens it and pastes back the code, the runner completes the exchange.

    POLLED, not pushed, and deliberately so — the same lesson as `menu-answers`:
    a control frame published while the runner's WS channel is down goes to a
    group with no consumer and is silently discarded, while the runner keeps
    heartbeating over REST and reads ONLINE the whole time.

    The minted token NEVER passes through the browser. The runner sends it here
    on completion and it is written straight into RunnerCredential, so the only
    secret a human handles is the single-use authorization code.
    """

    REQUESTED = "requested"          # a human asked; the runner has not started
    AWAITING_CODE = "awaiting_code"  # URL is up; waiting on the human
    COMPLETING = "completing"        # code submitted; the runner is exchanging
    DONE = "done"
    FAILED = "failed"
    STATUS_CHOICES = [
        (REQUESTED, "requested"), (AWAITING_CODE, "awaiting code"),
        (COMPLETING, "completing"), (DONE, "done"), (FAILED, "failed"),
    ]
    #: Terminal states — a mint in one of these is history, not work.
    FINISHED = (DONE, FAILED)

    #: Which subscription login the minted token replaces. Named by the human who
    #: started it, never inferred: a sign-in used to land in the primary
    #: unconditionally, so "add a fallback" silently overwrote the working login.
    PRIMARY = "primary"
    SECONDARY = "secondary"
    #: A long-lived token for the runner's credential list: a new one at the end,
    #: or (with `login_key`) a new value for that token.
    TOKEN = "token"
    #: Not a bundle slot: a FULL `claude auth login` the box keeps for itself.
    #: The only login that carries claude.ai connectors (a setup-token is
    #: inference-only), and its refresh token rotates, so no value ever comes
    #: back here — the runner reports `logged_in` and puts it first.
    BOX = "box"
    SLOT_CHOICES = [(PRIMARY, "primary"), (SECONDARY, "secondary"), (BOX, "box"), (TOKEN, "token")]

    # UUID pk like Runner/Turn: a mint id travels in URLs and logs, and a
    # sequential one would leak how many sign-ins the fleet has needed.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="mints")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=REQUESTED)
    slot = models.CharField(max_length=16, choices=SLOT_CHOICES, default=PRIMARY)
    #: For a `box` sign-in: which `RunnerLogin` it signs in. Blank is the
    #: box's default login (a mint from before there was a list). For a `token`
    #: sign-in: the token it replaces; blank adds a new one.
    login_key = models.CharField(max_length=32, blank=True, default="")
    authorize_url = models.TextField(blank=True, default="")
    # The single-use code a human pastes back. Cleared the moment the runner
    # takes it: it is spent on first use, and a code left lying in a row is a
    # credential nobody is accounting for.
    code = models.TextField(blank=True, default="")
    detail = models.TextField(blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["runner", "-created_at"])]

    def __str__(self) -> str:
        return f"mint:{self.runner_id}:{self.status}"


class SignInRequest(models.Model):
    """One device-code sign-in a box put in front of its owner (AWS SSO today).

    The push notification alone was not enough. Google accepting a push only
    means it will try to deliver it, and on 2026-10-19 three real requests were
    accepted and never shown on the owner's phone, with nothing anywhere else
    saying a box was waiting. So the request is also kept here, and the
    supervisor feed shows the owner every one still open.

    Nothing secret is added by keeping it. canopy-web already saw the URL when
    it built the push, and the code in it only signs the box in as whoever
    approves it, which is why only the owner is shown it, in the push or here.
    The row lives as long as the code: `url` is cleared the moment the box
    reports the outcome, and a row whose box never reported is treated as
    expired once `expires_at` passes. What stays is the audit line: who asked,
    on which box, and why.
    """

    PENDING = "pending"
    APPROVED = "approved"
    FAILED = "failed"
    EXPIRED = "expired"
    STATUS_CHOICES = [(PENDING, "pending"), (APPROVED, "approved"),
                      (FAILED, "failed"), (EXPIRED, "expired")]
    #: What the box may report. EXPIRED is also what an unreported row becomes.
    OUTCOMES = (APPROVED, FAILED, EXPIRED)

    # UUID pk: the id travels in URLs, and the box echoes it back with the outcome.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="sign_in_requests")
    #: The person it was put in front of: the runner's owner when it was asked.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    provider = models.CharField(max_length=16)
    url = models.TextField(blank=True, default="")
    label = models.CharField(max_length=60, blank=True, default="")
    requested_by = models.CharField(max_length=60, blank=True, default="")
    reason = models.CharField(max_length=200)
    #: Devices the push reached when it was asked.
    sent = models.IntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "status", "expires_at"])]

    def __str__(self) -> str:
        return f"sign-in:{self.runner_id}:{self.status}"


class RunnerDeviceKey(models.Model):
    """A hardware-bound (or software) key a runner proves possession of
    (Canopy Vault, canopy-web#1538 Phase 1a).

    The box generates an EC P-256 key — in the Secure Enclave or a TPM where it
    can — and registers only the PUBLIC half. It starts `pending`; a person who
    owns or administers the runner approves it from a browser (behind a passkey
    step-up), and from then on `/credentials/resolve` accepts a DPoP proof
    signed by it (RFC 9449), which a zero-knowledge agent's read requires.

    Identified by `jkt`, the RFC 7638 thumbprint, which is unique: re-posting the
    same public key is idempotent, and one key never belongs to two runners.

    `kind=aws-iam` (Phase 1b, canopy-web#1549) is an EC2 box's instance ROLE
    rather than a key: it has no JWK, stores `role_arn` (canonical, path-free)
    and its `jkt` is the sha256 of that ARN. It proves itself with a signed
    `sts:GetCallerIdentity` request (`apps/harness/aws_iam_proof.py`).
    """

    SECURE_ENCLAVE, TPM, SOFTWARE, AWS_IAM = "secure-enclave", "tpm", "software", "aws-iam"
    KIND_CHOICES = [(SECURE_ENCLAVE, "Secure Enclave"), (TPM, "TPM"), (SOFTWARE, "Software"),
                    (AWS_IAM, "AWS IAM role")]
    PENDING, ACTIVE, REVOKED = "pending", "active", "revoked"
    STATUS_CHOICES = [(PENDING, "Pending approval"), (ACTIVE, "Active"), (REVOKED, "Revoked")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="device_keys")
    #: The public JWK, reduced to its required members (kty, crv, x, y). Empty
    #: for `aws-iam`, which has no key.
    public_jwk = models.JSONField(default=dict, blank=True)
    #: `aws-iam` only: the IAM role, as `arn:aws:iam::<acct>:role/<name>`.
    role_arn = models.CharField(max_length=300, blank=True, default="")
    #: Zero-knowledge vault (canopy-web#1550): a SEPARATE P-256 key the box
    #: does ECDH with — Secure Enclave keys are per-purpose, so the signing key
    #: cannot also agree keys; for `aws-iam` it is a 0600 key file on the box.
    #: It is approved on its OWN (`ecdh_approved_at`): a new ECDH key on an
    #: active key waits for a person, but the signing key stays active, so the
    #: box keeps signing in and running every other agent meanwhile.
    ecdh_public_jwk = models.JSONField(default=dict, blank=True)
    #: RFC 7638 thumbprint of `ecdh_public_jwk` — bound into every device wrap.
    ecdh_jkt = models.CharField(max_length=64, blank=True, default="", db_index=True)
    #: When a person approved THIS ecdh key (None: not yet, or none). Vault keys
    #: are wrapped, and values sealed, only to an approved one (`ecdh_ready`).
    ecdh_approved_at = models.DateTimeField(null=True, blank=True)
    ecdh_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                         null=True, blank=True, related_name="+")
    #: Where the box SAYS the ECDH private key lives — "secure-enclave", "tpm",
    #: "aws-kms" or "software" (a 0600 file); "" = not said. Self-reported and
    #: shown to people only ("key held by AWS"); nothing trusts it.
    ecdh_store = models.CharField(max_length=16, blank=True, default="")
    jkt = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    label = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"devicekey:{self.runner_id}:{self.jkt[:12]}:{self.status}"

    @property
    def ecdh_ready(self) -> bool:
        """May vault keys be wrapped (and values sealed) to this key's ECDH key?"""
        return self.status == self.ACTIVE and bool(self.ecdh_jkt) and self.ecdh_approved_at is not None

    @property
    def ecdh_waiting(self) -> bool:
        """Active, signing in, but its (new) ECDH key awaits a person's approval."""
        return self.status == self.ACTIVE and bool(self.ecdh_jkt) and self.ecdh_approved_at is None


class RunnerAccessToken(models.Model):
    """A runner's API credential, bought with its device key — never a person's PAT.

    A box proves itself with an APPROVED `RunnerDeviceKey` (a DPoP proof from a key
    it holds, or its EC2 instance role) at `POST /api/runner-sessions/`, and gets
    one of these back: an hour long, bound to that runner, and good only on the
    runner protocol (`runner_auth.reaches`). When it runs out the box proves itself
    again. So nothing long-lived sits on the box to leak, and nothing a person
    tidies up in Settings → Tokens can cut a runner off by accident — the way a
    revoked PAT silently swallowed cloud-ec2-2's turns (2026-10-17). Revoking the
    device key, retiring the runner or deactivating its owner kills every token
    at once: `lookup` re-checks all three on each use.

    It authenticates AS the runner's owner, because every runner gate is
    owner-scoped (`Runner.owner`, `runner_may_hold_agent`); what it may reach is
    narrowed by path, not by a second identity.
    """

    PREFIX = "crt_"
    TTL = dt.timedelta(hours=1)

    token_hash = models.CharField(max_length=64, unique=True)
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="access_tokens")
    device_key = models.ForeignKey(RunnerDeviceKey, on_delete=models.CASCADE,
                                   related_name="access_tokens")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["expires_at"])]

    def __str__(self) -> str:
        return f"runnertoken:{self.runner_id}:{self.expires_at:%Y-%m-%dT%H:%M}"

    @classmethod
    def mint(cls, device_key: RunnerDeviceKey) -> tuple[str, RunnerAccessToken]:
        """A fresh token for the key's runner. The raw value is returned once."""
        import secrets

        raw = cls.PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(
            token_hash=hash_token(raw),
            runner_id=device_key.runner_id, device_key=device_key,
            expires_at=timezone.now() + cls.TTL)
        # Tokens past their hour authenticate nothing; drop them as new ones arrive
        # rather than letting a box's hourly refresh grow the table forever.
        cls.objects.filter(runner_id=device_key.runner_id,
                           expires_at__lt=timezone.now() - cls.TTL).delete()
        return raw, token

    @classmethod
    def lookup(cls, raw: str) -> RunnerAccessToken | None:
        """The live token for `raw`: unexpired, its device key still ACTIVE, its
        runner not retired, its owner an active person. Else None."""
        if not raw or not raw.startswith(cls.PREFIX):
            return None
        return (
            cls.objects.select_related("runner__owner", "device_key")
            .filter(token_hash=hash_token(raw),
                    expires_at__gt=timezone.now(),
                    device_key__status=RunnerDeviceKey.ACTIVE,
                    runner__owner__is_active=True)
            .exclude(runner__status=Runner.RETIRED)
            .first()
        )


class RunnerCredentialSet(models.Model):
    """One ordered list of Claude credentials that several cloud runners read as their
    own: every member of a fleet (`RunnerFleet.credential_set`), however many there are
    and however often they come and go, and any other cloud runner of the set's owner
    that joins it (`apps/harness/credential_sets.py`).

    The entries are `RunnerLogin` rows on the set (tokens and API keys only — a full
    login's refresh token rotates, so it cannot be shared and would be state on one
    box). Their VALUES live in the set's vaults, ONE PER WORKSPACE TREE its runners are
    in: a vault never serves two isolation boundaries (docs/architecture/vault.md § A
    runner's own vault), so the list is shared across trees and the values are not —
    each tree's vault holds its own copy, sealed by a person or a box of that tree.
    Usage caps are per Claude account: runners on one token share one cap, so a set
    holds tokens from several accounts.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="runner_credential_sets")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    #: Where the entries' values are sealed: at most one vault per workspace tree, each
    #: made (or adopted) with the set's first runner in that tree
    #: (`credential_sets.attach_member`), which a vault for runners needs to be attached to.
    vaults = models.ManyToManyField("agents.Vault", related_name="+", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"credential-set:{self.name}"
