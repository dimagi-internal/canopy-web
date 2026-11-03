"""Runner fleets, holds, invitations, pairings and private runners.

Framework tier. Split from the former models.py; `apps.harness.models` re-exports.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .credentials import RunnerCredentialSet
from .runner import Runner


class FleetHold(models.Model):
    """The fleet-wide hold: while `held`, NO runner is handed a turn — anywhere.

    The per-runner pause (`Runner.paused`) parks one box; this parks the whole fleet,
    for the moment something is starting sessions nobody can account for (2026-10-07:
    people-digest turns appearing on a laptop). Turns keep ENQUEUING while held — that
    is the point: they pile up QUEUED, each carrying its origin, `origin_ref.trigger`
    and initiator, so `/harness/turns?status=queued` is the list of everything that
    tried to start, and `unclaimable_queued_turns` names the hold as the reason.

    Enforced in ONE place, `claim.claim_next_turn` (both the HTTP and the WebSocket
    claim run through it), so it binds every runner regardless of its binary. Like the
    runner pause it stops STARTING work, never finishing it: an executing turn keeps
    its lease, and no NEW claim, ride or pin is granted while held.

    What it cannot reach, because none of it passes through a claim: a laptop emdash
    session that keeps working after its turn was closed on delivery (stop that with
    `interrupt_session`); a menu answer pressed into a live session; and `claude -p`
    run directly by canopy CLI tools or a local launchd job.

    A single row (pk=1). Set and released only by a superuser — it spans every tenant.
    """

    SINGLETON_PK = 1

    held = models.BooleanField(default=False)
    note = models.CharField(max_length=500, blank=True, default="")
    held_at = models.DateTimeField(null=True, blank=True)
    held_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="+")

    @classmethod
    def current(cls) -> "FleetHold":
        row, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return row

    @classmethod
    def is_held(cls) -> bool:
        return cls.objects.filter(pk=cls.SINGLETON_PK, held=True).exists()


#: How long a pairing invitation stays open: long enough for `up.sh` to bring a
#: box up and the operator to touch their passkey, short enough that a forgotten
#: one is no standing door.
INVITATION_TTL = dt.timedelta(hours=1)


class RunnerInvitation(models.Model):
    """A person invites ONE new cloud box, named, into ONE workspace, bound to the
    box's EC2 instance role — so the box pairs holding no long-lived secret.

    `up.sh` creates it with the operator's own token once the stack's role exists.
    The operator approves it from a browser, behind a passkey step-up
    (`/api/harness/runner-invitations/{id}/approve`) — a token cannot, so neither
    `up.sh` nor the box can approve its own. The box then proves its role
    (`POST /api/harness/runners/pair-by-role`, `aws_iam_proof`) and canopy-web
    creates the Runner (owned by the invitation's creator, in its workspace) with
    an ACTIVE aws-iam device key, and marks the invitation consumed. Single use.

    One live (unconsumed) invitation per (role, name): a new one replaces it.
    """

    PENDING, APPROVED, CONSUMED, EXPIRED = "pending", "approved", "consumed", "expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: The Runner.name the box will pair as (`runner/ec2/_lib.sh::runner_name_ok`).
    name = models.CharField(max_length=63)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="runner_invitations")
    #: The person inviting the box: they approve it, and they own the runner.
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                   related_name="+")
    #: The box's instance role, canonical (`aws_iam_proof.canonical_role_arn`).
    role_arn = models.CharField(max_length=300)
    #: sha256 of `role_arn` — the `jkt` the box's aws-iam device key will carry.
    role_jkt = models.CharField(max_length=64, db_index=True)
    #: The box's zero-knowledge ECDH public key (the `<stack>-ecdh` KMS key), when
    #: `up.sh` could read it: one approval covers both. {} when none.
    ecdh_public_jwk = models.JSONField(default=dict, blank=True)
    #: The runners the machine will pair as
    #: (`docs/architecture/runners.md § Several runners on one machine`): `[{name, workspace, kind, slug,
    #: ecdh_public_jwk, runner_id}]`. `kind` is "tree" or "agent" (`slug` names the
    #: agent). Empty for a single-runner invitation, which pairs as `name` in
    #: `workspace` by its instance role (`invited_runners`). `runner_id` is filled in when
    #: the box pairs.
    runners = models.JSONField(default=list, blank=True)
    #: Pair its runners directed-only (`Runner.directed`): a test box (`up.sh
    #: --test`) is directed from the moment it pairs, before anyone could route to it.
    directed = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    consumed_at = models.DateTimeField(null=True, blank=True)
    runner = models.ForeignKey(Runner, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="+")
    #: A FLEET's invitation (`fleets.py`): approving it activates the fleet, and it
    #: is used up by that approval — its members join on AWS's word, never on it.
    fleet = models.ForeignKey("RunnerFleet", on_delete=models.CASCADE, null=True, blank=True,
                              related_name="invitations")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["role_jkt", "name"], condition=models.Q(consumed_at__isnull=True),
                                    name="one_live_runner_invitation_per_role_and_name"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"runner-invitation:{self.name}:{self.status}"

    def save(self, *args, **kwargs):
        if self.expires_at is None:
            self.expires_at = (self.created_at or timezone.now()) + INVITATION_TTL
        super().save(*args, **kwargs)

    @property
    def expired(self) -> bool:
        return self.consumed_at is None and timezone.now() >= self.expires_at

    @property
    def status(self) -> str:
        if self.consumed_at is not None:
            return self.CONSUMED
        if self.expired:
            return self.EXPIRED
        return self.APPROVED if self.approved_at is not None else self.PENDING


#: How long a laptop pairing waits for its approval and claim.
PAIRING_TTL = dt.timedelta(minutes=15)


#: Added to `expires_at` on approval, so a slow claim after a late approval works.
PAIRING_APPROVAL_GRACE = dt.timedelta(minutes=10)


class RunnerFleet(models.Model):
    """A set of interchangeable cloud runners: the instances of ONE EC2 Auto Scaling
    group, which canopy-web grows and shrinks with the work waiting for it
    (`apps/harness/fleets.py`; spec 2026-10-24-runner-fleets-design.md).

    A person approves the fleet ONCE, from a browser behind a passkey (its
    `RunnerInvitation`); no token can. After that an instance joins on its own when
    AWS says it is the fleet's: STS that it signs as `assumed-role/<role>/<i-…>`,
    EC2 that the instance carries the group's reserved `aws:autoscaling:groupName`
    tag. Each member is its own runner with its own device key (role + instance id);
    all of them open vaults with the fleet's one KMS ECDH key, which only the role
    can use, and read the fleet's credential set.
    """

    PENDING, ACTIVE, RETIRED = "pending", "active", "retired"
    STATUS_CHOICES = [(PENDING, "Waiting for approval"), (ACTIVE, "Active"), (RETIRED, "Retired")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: A slug, and the prefix of every member's name (`<name>-<instance id>`).
    name = models.CharField(max_length=40, unique=True)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="runner_fleets")
    #: Its creator, who approves it and owns every member.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    #: The members' instance role, canonical (`aws_iam_proof.canonical_role_arn`).
    role_arn = models.CharField(max_length=300)
    role_jkt = models.CharField(max_length=64, unique=True)
    #: The fleet's ECDH public key (its KMS key), approved with the fleet.
    ecdh_public_jwk = models.JSONField(default=dict, blank=True)
    region = models.CharField(max_length=32)
    group_name = models.CharField(max_length=255)
    min_members = models.PositiveSmallIntegerField(default=1)
    max_members = models.PositiveSmallIntegerField(default=4)
    #: Turns one member runs at once — how many waiting turns one more member absorbs.
    member_turns = models.PositiveSmallIntegerField(default=2)
    credential_set = models.ForeignKey(RunnerCredentialSet, on_delete=models.PROTECT, related_name="fleets")
    #: The fleet's own copy of each vault key wrapped to its ECDH key,
    #: `{vault_id: {"kv": n, "wrap": …}}`: what a new member is given (`fleets.join`).
    #: Ciphertext for a key only the fleet's role can use; canopy-web opens none of it.
    vault_wraps = models.JSONField(default=dict, blank=True)
    #: The fleet's routing: every row an agent's runner list or a workspace's runner
    #: order holds for its members, `{key: fields}` (`fleets.sync_routing`). Members are
    #: routed as the fleet, so a new member joins with these rows and a change to one
    #: member's rows reaches all of them; kept here so it outlives every member.
    routing = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover
        return f"fleet:{self.name}"


class RunnerPairing(models.Model):
    """A laptop box asks to be paired, and a person approves it in their browser
    (`apps/harness/api_runner_pairings.py`, spec 2026-10-19-runner-installers).

    The box makes its device key first and opens a pairing with a DPoP proof of
    it (anonymous: no token exists yet). The person opens `/pair/<user_code>`,
    picks a workspace and a name, and approves behind a passkey step-up. The
    approval's one-time secret goes back ONLY through a redirect to the box's
    loopback (`redirect_port`), so whoever approves must be at that machine. The
    box then claims with the secret and a proof of the same key, and gets a
    Runner whose device key is already ACTIVE. Single use; one live pairing per
    key (`jkt`).
    """

    PENDING, APPROVED, CONSUMED, DECLINED, EXPIRED = "pending", "approved", "consumed", "declined", "expired"
    PLATFORMS = ("macos", "windows", "linux")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: What the person checks against the box's window: `ABCD-EFGH`.
    user_code = models.CharField(max_length=9, unique=True)
    #: The box's signing key (reduced public JWK) and its RFC 7638 thumbprint.
    public_jwk = models.JSONField(default=dict)
    jkt = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=16)
    ecdh_public_jwk = models.JSONField(default=dict, blank=True)
    ecdh_store = models.CharField(max_length=16, blank=True, default="")
    #: Self-reported by the box: shown to the approver, trusted for nothing.
    hostname = models.CharField(max_length=100, blank=True, default="")
    account = models.CharField(max_length=100, blank=True, default="")
    platform = models.CharField(max_length=10)
    #: The runner it makes drives emdash (else Claude CLI): an emdash the box can
    #: actually drive, as `api_runner_pairings.drives_emdash` decides it.
    has_emdash = models.BooleanField(default=False)
    redirect_port = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, null=True, blank=True,
                                  related_name="+")
    #: The Runner.name the approver chose.
    name = models.CharField(max_length=63, blank=True, default="")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    #: sha256 of the approval's one-time secret; the raw value is only ever in
    #: the loopback redirect.
    secret_hash = models.CharField(max_length=64, blank=True, default="")
    declined_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    runner = models.ForeignKey(Runner, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    #: The runner this box says it already IS (its runner.json), when its token stopped
    #: working: approved by that runner's owner, the claim adds the key to that runner
    #: instead of making a new one. The box's word alone changes nothing (canopy-web#1705).
    reconnect_runner = models.ForeignKey(Runner, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="+")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"runner-pairing:{self.user_code}:{self.status}"

    def save(self, *args, **kwargs):
        if self.expires_at is None:
            self.expires_at = (self.created_at or timezone.now()) + PAIRING_TTL
        super().save(*args, **kwargs)

    @property
    def status(self) -> str:
        if self.consumed_at is not None:
            return self.CONSUMED
        if self.declined_at is not None:
            return self.DECLINED
        if timezone.now() >= self.expires_at:
            return self.EXPIRED
        return self.APPROVED if self.approved_at is not None else self.PENDING


class PrivateRunner(models.Model):
    """A PERSON's own machine: plain Claude Code with the canopy plugin, no runner app.

    On its own it never claims a turn and nothing routes to it. It exists so a session a person
    opens there with an agent is a real, recorded turn (`Turn.ORIGIN_LOCAL`,
    `apps/harness/local.py`): who ran it, on which machine, under which license.
    It belongs to the person, not to an agent or a workspace — one private runner serves
    every agent they use. canopy-web#1704. When the machine also runs the runner app,
    `runner` links the two, and that runner is marked `private` (canopy-web#1705).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="private_runners")
    #: What the person calls it (`canopy private-runner register --name`), e.g. aath-mbp.
    name = models.CharField(max_length=100)
    #: The hostname it registered from — a hint for the person, never an identity.
    host = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    #: Last time a direct turn opened or renewed from it.
    last_seen_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    #: The runner this machine also is (canopy-web#1705): one machine, one object with
    #: two powers — direct sessions here, and the runner app's turns. Its device key is
    #: what a direct turn proves itself with.
    runner = models.OneToOneField("harness.Runner", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="private_runner")

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="one_private_runner_name_per_owner"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"private_runner:{self.name}"
