# Workspace Email Domains Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any workspace admin bring a domain to canopy-web and run its email through Amazon SES — agent addresses become two-way email turns, every other address forwards to a canopy user.

**Architecture:** A new, removable product-tier Django app `apps/mail/`. SES receives mail into S3 and notifies canopy-web over SNS; canopy-web verifies, parses, threads, and either stores it + enqueues the agent's email turn or forwards it through SES. Agents read/send through REST routes (MCP for free). Domains, DNS status and routes are a Settings → Email page. One new CloudFormation stack holds the AWS side. The canopy CLI gets a per-address backend switch in a separate PR.

**Tech Stack:** Django 5 + Django Ninja + Pydantic v2, PostgreSQL, boto3 (`sesv2`, `s3`) tested with `botocore.stub.Stubber`, `dnspython`, `cryptography` (SNS signatures), stdlib `email`; React 19 + Vite + Tailwind 4 + shadcn/ui; CloudFormation.

**Spec:** `docs/superpowers/specs/2026-10-12-workspace-email-domains-design.md` — read it before any task.

## Global Constraints

- `apps/mail` is **PRODUCT** tier: add `"mail"` to `PRODUCT` in `tests/test_architecture_boundary.py` and `ARCHITECTURE.md`. No framework app may import it.
- It must be removable: no edits to framework apps except (a) the server-only `authserv_id` kwarg on `apps.harness.services.enqueue_turn` (Task 8), (b) registry lines in `apps/api/api.py`, `apps/api/route_gates.py`, `config/settings/base.py`, `apps/common/middleware.py` (allowlist).
- Gate domain/route admin on `perms.INTEGRATIONS`; agent mailbox use on `perms.AGENT_WORK`; viewing threads on membership (`READ`). Never name a role outside `apps/workspaces`.
- Every write route is in `route_gates.ROUTE_GATES` and in `SETTINGS` (domain/route writes, with `audit.note_change`) or `NOT_SETTINGS` (everything else).
- Forwarding destinations are **`User` FKs of workspace members only** — no free-text destination anywhere.
- Routing is inert until the domain is `verified`. Domain names are globally unique.
- Retention: `incoming/` S3 objects expire after **7 days**; agent mail is copied to `kept/` (no expiry). Forwarded bodies are never stored in Postgres.
- Reputation: per-domain `daily_send_cap` default **200**; auto-suspend over 7 days with **≥ 50** sends when bounces **> 4 %** or complaints **> 0.08 %**, or **> 10 hard bounces in an hour**.
- Reserved local part: **`canopy-forwarder`** (From of forwarded mail; cannot be routed).
- SES region/account: **us-east-1 / 858923557655** (labs). Inbound MX: **`inbound-smtp.us-east-1.amazonaws.com`**. Configuration set: **`canopy-tenant-mail`**. Trusted authserv-id for SES mail: **`amazonses.com`**.
- `MAIL_ENABLED` setting defaults **False**; every inbound/send path refuses (and says so) when off.
- Changed any `schemas.py`/`api.py`: `cd frontend && npm run gen:api:local` and commit `generated.ts`.
- Frontend: semantic tokens only (no raw palette classes, no `dark:`); dense tables, not cards.
- Ship per canopy-web CLAUDE.md: PR → `gh pr merge <n> --auto` (no strategy flag) → deploy workflow by hand.

## Review Focus

1. **A domain whose DNS already has SPF/MX for another provider** (Zoho): the page must show a *merged* SPF and name the current MX provider, never suggest a second `v=spf1` record (two SPF records = permerror = all mail fails). Test in Task 3.
2. **SNS redelivery / duplicate notifications** for the same message: must not create two messages, two turns, or two forwards. Test in Tasks 6 and 7 (`message_id` dedupe + `MailForwardLog` unique on `(domain, message_id)`).
3. **Mail with no `Message-ID`, non-UTF-8 / RFC 2047 encoded headers, or only an HTML part**: must parse, thread, and render; a missing Message-ID gets a synthesized stable one (`<sha256-of-raw@canopy>`). Test in Task 5.
4. **A recipient list with several of our addresses** (`To: ada@, jjackson@`): each recipient routes independently, exactly once each; the agent's own address in From never wakes it. Test in Task 6.
5. **A route pointing at a user who later leaves the workspace**: forwarding must stop (drop + log "destination is no longer a member"), not keep mailing an ex-member. Test in Task 7.

---

## File map

```
apps/mail/
  __init__.py, apps.py            app config (label "mail")
  models.py                       MailDomain, MailRoute, MailThread, MailMessage, MailAttachment, MailForwardLog, MailEvent
  migrations/0001_initial.py      generated
  ses.py                          the ONLY boto3 caller: identities, raw send, S3 get/copy
  dns.py                          expected records + live check + MX provider naming
  domains.py                      add / refresh / remove a domain; route CRUD; address → route
  sns.py                          SNS message signature verification + subscription confirm
  parse.py                        raw MIME → Parsed (stdlib email)
  threads.py                      thread placement by Message-ID / In-Reply-To / References
  inbound.py                      one SNS notification → per-recipient outcome
  forward.py                      build + send a forwarded copy
  agent_mail.py                   store agent mail + enqueue the email turn; agent send/reply
  reputation.py                   SES events → delivery status, counters, suspension
  schemas.py, api.py              REST
  management/commands/mail_refresh.py
deploy/aws/canopy-mail.cfn.yaml   bucket, topics, receipt rule set, config set, IAM
frontend/src/api/mail.ts
frontend/src/pages/WorkspaceEmailPage.tsx (+ .test.tsx)
frontend/src/components/agent/AgentMailSection.tsx
docs/architecture/mail.md, docs/runbooks/mail-domain-onboarding.md
tests/test_mail_*.py
```

---

### Task 1: App skeleton, models, settings, tiering

**Files:**
- Create: `apps/mail/__init__.py`, `apps/mail/apps.py`, `apps/mail/models.py`, `apps/mail/migrations/__init__.py`
- Modify: `config/settings/base.py` (INSTALLED_APPS + a `# --- Mail domains (apps/mail) ---` block), `tests/test_architecture_boundary.py:29`, `ARCHITECTURE.md` (tier list)
- Test: `tests/test_mail_models.py`

**Interfaces:**
- Produces: models below; settings `MAIL_ENABLED`, `MAIL_SES_REGION`, `MAIL_INBOUND_BUCKET`, `MAIL_INBOUND_TOPIC_ARN`, `MAIL_EVENTS_TOPIC_ARN`, `MAIL_CONFIGURATION_SET`, `MAIL_SES_AUTHSERV_ID`, `MAIL_INBOUND_MX`, `MAIL_FORWARDER_LOCAL_PART`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_models.py
"""Mail models: the invariants the database itself must hold."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from apps.agents.models import Agent
from apps.mail.models import MailDomain, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def ws():
    u = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    return Workspace.objects.create(slug="marshellis", display_name="M", created_by=u)


def test_domain_name_is_globally_unique(ws):
    other = Workspace.objects.create(slug="other", display_name="O", created_by=ws.created_by)
    MailDomain.objects.create(workspace=ws, name="marshellis.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        MailDomain.objects.create(workspace=other, name="marshellis.com")


def test_one_route_per_local_part(ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com")
    MailRoute.objects.create(domain=d, local_part="*", kind=MailRoute.DROP)
    with pytest.raises(IntegrityError), transaction.atomic():
        MailRoute.objects.create(domain=d, local_part="*", kind=MailRoute.DROP)


def test_message_id_unique_per_mailbox(ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws)
    r = MailRoute.objects.create(domain=d, local_part="ada", kind=MailRoute.AGENT, agent=a)
    t = MailThread.objects.create(workspace=ws, route=r, subject="hi")
    MailMessage.objects.create(thread=t, direction=MailMessage.IN, message_id="<x@y>")
    with pytest.raises(IntegrityError), transaction.atomic():
        MailMessage.objects.create(thread=t, direction=MailMessage.IN, message_id="<x@y>")
    b = Agent.objects.create(slug="mbob", name="Bob", workspace=ws)
    rb = MailRoute.objects.create(domain=d, local_part="bob", kind=MailRoute.AGENT, agent=b)
    tb = MailThread.objects.create(workspace=ws, route=rb, subject="hi")
    MailMessage.objects.create(thread=tb, direction=MailMessage.IN, message_id="<x@y>")  # same mail, other agent: fine


def test_mail_is_off_by_default(settings):
    assert settings.MAIL_ENABLED is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_mail_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.mail'`

- [ ] **Step 3: Implement**

```python
# apps/mail/apps.py
from django.apps import AppConfig


class MailConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mail"
    label = "mail"
```

```python
# apps/mail/models.py
"""Workspace email domains: a workspace's own domain, run through Amazon SES.

PRODUCT tier and removable: nothing in a framework app imports this module.
Removing the app drops these tables and nothing else notices. See
docs/superpowers/specs/2026-10-12-workspace-email-domains-design.md.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class MailDomain(models.Model):
    PENDING, VERIFIED, SUSPENDED = "pending", "verified", "suspended"
    STATUS_CHOICES = [(PENDING, "Pending DNS"), (VERIFIED, "Verified"), (SUSPENDED, "Suspended")]

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="mail_domains")
    name = models.CharField(max_length=253, unique=True)
    """Lowercased. Globally unique: an SES identity is account-wide, so one domain
    can belong to one workspace only."""
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    receiving = models.BooleanField(default=False)
    """Whether the domain's MX points at SES right now (live check, cached)."""
    mx_provider = models.CharField(max_length=120, blank=True, default="")
    """Who receives mail for the domain today, named for the cut-over warning."""
    mail_from_subdomain = models.CharField(max_length=63, default="bounce")
    dkim_tokens = models.JSONField(default=list, blank=True)
    suspended_reason = models.TextField(blank=True, default="")
    daily_send_cap = models.PositiveIntegerField(default=200)
    checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    @property
    def routes_live(self) -> bool:
        """Routing is inert until DKIM verifies: that is the proof of DNS control."""
        return self.status in (self.VERIFIED, self.SUSPENDED)


class MailRoute(models.Model):
    AGENT, FORWARD, DROP = "agent", "forward", "drop"
    KIND_CHOICES = [(AGENT, "Agent mailbox"), (FORWARD, "Forward to a member"), (DROP, "Drop")]
    CATCH_ALL = "*"

    domain = models.ForeignKey(MailDomain, on_delete=models.CASCADE, related_name="routes")
    local_part = models.CharField(max_length=64)
    """Lowercased; `*` is the catch-all. Exact match wins over `*`."""
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    agent = models.ForeignKey("agents.Agent", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="mail_routes")
    forward_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.CASCADE, related_name="mail_forwards")
    """A canopy USER, never a typed address — the feature cannot be used as a relay."""
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["domain__name", "local_part"]
        constraints = [
            models.UniqueConstraint(fields=["domain", "local_part"], name="mail_route_unique_local"),
            models.CheckConstraint(
                name="mail_route_target_matches_kind",
                condition=(
                    models.Q(kind="agent", agent__isnull=False, forward_to__isnull=True)
                    | models.Q(kind="forward", agent__isnull=True, forward_to__isnull=False)
                    | models.Q(kind="drop", agent__isnull=True, forward_to__isnull=True)
                ),
            ),
        ]

    @property
    def address(self) -> str:
        return f"{self.local_part}@{self.domain.name}"


class MailThread(models.Model):
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="mail_threads")
    route = models.ForeignKey(MailRoute, on_delete=models.CASCADE, related_name="threads")
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    """The `thread_id` turns and agents carry."""
    subject = models.CharField(max_length=998, blank=True, default="")
    message_count = models.PositiveIntegerField(default=0)
    last_message_at = models.DateTimeField(null=True, blank=True)
    archived = models.BooleanField(default=False)
    unread = models.BooleanField(default=True)

    class Meta:
        ordering = ["-last_message_at"]


class MailMessage(models.Model):
    IN, OUT = "in", "out"
    SENT, DELIVERED, BOUNCED, COMPLAINED, REJECTED = "sent", "delivered", "bounced", "complained", "rejected"

    thread = models.ForeignKey(MailThread, on_delete=models.CASCADE, related_name="messages")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="+", null=True)
    """Denormalised from thread for the per-workspace unique constraint."""
    direction = models.CharField(max_length=3, choices=[(IN, "in"), (OUT, "out")])
    mailbox = models.CharField(max_length=320, default="")
    """The route address this copy belongs to. One email to two agents is two rows —
    dedupe is per mailbox, so the second agent is never told it is a duplicate."""
    message_id = models.CharField(max_length=998)
    in_reply_to = models.CharField(max_length=998, blank=True, default="")
    references = models.TextField(blank=True, default="")
    from_addr = models.CharField(max_length=320, blank=True, default="")
    from_name = models.CharField(max_length=320, blank=True, default="")
    to = models.JSONField(default=list, blank=True)
    cc = models.JSONField(default=list, blank=True)
    subject = models.CharField(max_length=998, blank=True, default="")
    text = models.TextField(blank=True, default="")
    html = models.TextField(blank=True, default="")
    raw_key = models.CharField(max_length=512, blank=True, default="")
    verdicts = models.JSONField(default=dict, blank=True)
    auth_results = models.TextField(blank=True, default="")
    ses_message_id = models.CharField(max_length=128, blank=True, default="")
    delivery_status = models.CharField(max_length=12, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "mailbox", "message_id"], name="mail_message_unique_id"),
        ]

    def save(self, *args, **kwargs):
        if self.workspace_id is None and self.thread_id:
            self.workspace_id = self.thread.workspace_id
        if not self.mailbox and self.thread_id:
            self.mailbox = self.thread.route.address
        super().save(*args, **kwargs)


class MailAttachment(models.Model):
    message = models.ForeignKey(MailMessage, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=255, blank=True, default="")
    size = models.PositiveIntegerField(default=0)
    s3_key = models.CharField(max_length=512)


class MailForwardLog(models.Model):
    """One row per forwarded message — no body: canopy keeps no human mail it only passes on."""
    FORWARDED, FAILED, DROPPED = "forwarded", "failed", "dropped"

    domain = models.ForeignKey(MailDomain, on_delete=models.CASCADE, related_name="forwards")
    route = models.ForeignKey(MailRoute, null=True, on_delete=models.SET_NULL, related_name="+")
    message_id = models.CharField(max_length=998)
    recipient = models.CharField(max_length=320)
    original_from = models.CharField(max_length=320, blank=True, default="")
    subject = models.CharField(max_length=998, blank=True, default="")
    destination = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                    related_name="+")
    status = models.CharField(max_length=10)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["domain", "message_id", "recipient"],
                                    name="mail_forward_once"),
        ]


class MailEvent(models.Model):
    """An SES sending event, kept for the reputation window (7 days is all that is read)."""
    domain = models.ForeignKey(MailDomain, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=16)  # Send | Delivery | Bounce | Complaint | Reject
    hard = models.BooleanField(default=False)  # a Permanent bounce
    ses_message_id = models.CharField(max_length=128, blank=True, default="")
    at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["domain", "kind", "at"])]
```

Settings block (append after the retention block in `config/settings/base.py`, and add `"apps.mail",` to `INSTALLED_APPS` after `"apps.retention",`):

```python
# --- Mail domains (apps/mail) ---
# A workspace's own domain, run through SES in the labs account
# (deploy/aws/canopy-mail.cfn.yaml). OFF until the stack exists: every inbound
# and send path refuses, and says so, while this is false.
MAIL_ENABLED = env.bool("MAIL_ENABLED", default=False)
MAIL_SES_REGION = env("MAIL_SES_REGION", default=CANOPY_SES_REGION)
MAIL_INBOUND_BUCKET = env("MAIL_INBOUND_BUCKET", default="")
MAIL_INBOUND_TOPIC_ARN = env("MAIL_INBOUND_TOPIC_ARN", default="")
MAIL_EVENTS_TOPIC_ARN = env("MAIL_EVENTS_TOPIC_ARN", default="")
MAIL_CONFIGURATION_SET = env("MAIL_CONFIGURATION_SET", default="canopy-tenant-mail")
# OUR receiver for SES mail — the one Authentication-Results header we trust.
MAIL_SES_AUTHSERV_ID = env("MAIL_SES_AUTHSERV_ID", default="amazonses.com")
MAIL_INBOUND_MX = env("MAIL_INBOUND_MX", default="inbound-smtp.us-east-1.amazonaws.com")
MAIL_FORWARDER_LOCAL_PART = "canopy-forwarder"
```

(If `CANOPY_SES_REGION` is defined later in the file than this block, place the block after it.)

Tiering: in `tests/test_architecture_boundary.py` change `PRODUCT = {"projects",` line to include `"mail"`, and add `mail` to the PRODUCT list in `ARCHITECTURE.md`.

- [ ] **Step 4: Make migration, run tests**

Run: `uv run python manage.py makemigrations mail && uv run pytest tests/test_mail_models.py tests/test_architecture_boundary.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/mail config/settings/base.py tests/test_mail_models.py tests/test_architecture_boundary.py ARCHITECTURE.md
git commit -m "feat(mail): app skeleton and models for workspace email domains"
```

---

### Task 2: The SES/S3 seam (`ses.py`)

**Files:**
- Create: `apps/mail/ses.py`
- Test: `tests/test_mail_ses.py`

**Interfaces:**
- Produces:
  - `create_identity(domain: str, *, mail_from: str) -> list[str]` (DKIM tokens)
  - `identity_verified(domain: str) -> bool`
  - `delete_identity(domain: str) -> None`
  - `send_raw(*, from_addr: str, to: list[str], raw: bytes) -> str` (SES MessageId)
  - `get_object(key: str) -> bytes`; `keep(key: str) -> str` (copies `incoming/x` → `kept/x`, returns new key); `put_object(key: str, data: bytes, content_type: str) -> None`; `presign(key: str, filename: str) -> str`
  - `class MailOff(Exception)` — raised by every function when `MAIL_ENABLED` is false.
  - Module-level `_sesv2()` / `_s3()` factories tests replace.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_ses.py
from __future__ import annotations

import boto3
import pytest
from botocore.stub import ANY, Stubber

from apps.mail import ses


@pytest.fixture()
def on(settings):
    settings.MAIL_ENABLED = True
    settings.MAIL_INBOUND_BUCKET = "canopy-inbound-mail-test"
    settings.MAIL_CONFIGURATION_SET = "canopy-tenant-mail"


@pytest.fixture()
def sesv2(monkeypatch):
    client = boto3.client("sesv2", region_name="us-east-1",
                          aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(ses, "_sesv2", lambda: client)
    with Stubber(client) as stub:
        yield stub


def test_everything_refuses_when_mail_is_off(settings):
    settings.MAIL_ENABLED = False
    with pytest.raises(ses.MailOff):
        ses.send_raw(from_addr="a@b.c", to=["d@e.f"], raw=b"x")


def test_create_identity_returns_dkim_tokens_and_sets_mail_from(on, sesv2):
    sesv2.add_response("create_email_identity",
                       {"IdentityType": "DOMAIN", "VerifiedForSendingStatus": False,
                        "DkimAttributes": {"Tokens": ["t1", "t2", "t3"]}},
                       {"EmailIdentity": "marshellis.com", "ConfigurationSetName": "canopy-tenant-mail"})
    sesv2.add_response("put_email_identity_mail_from_attributes", {},
                       {"EmailIdentity": "marshellis.com", "MailFromDomain": "bounce.marshellis.com",
                        "BehaviorOnMxFailure": "USE_DEFAULT_VALUE"})
    assert ses.create_identity("marshellis.com", mail_from="bounce.marshellis.com") == ["t1", "t2", "t3"]


def test_send_raw_always_carries_the_tenant_configuration_set(on, sesv2):
    sesv2.add_response("send_email", {"MessageId": "ses-1"},
                       {"FromEmailAddress": "ada@marshellis.com",
                        "Destination": {"ToAddresses": ["x@y.z"]},
                        "Content": {"Raw": {"Data": b"raw"}},
                        "ConfigurationSetName": "canopy-tenant-mail"})
    assert ses.send_raw(from_addr="ada@marshellis.com", to=["x@y.z"], raw=b"raw") == "ses-1"


def test_identity_verified_reads_dkim_status(on, sesv2):
    sesv2.add_response("get_email_identity",
                       {"DkimAttributes": {"Status": "SUCCESS"}, "VerifiedForSendingStatus": True},
                       {"EmailIdentity": "marshellis.com"})
    assert ses.identity_verified("marshellis.com") is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_mail_ses.py -v` — Expected: FAIL (`cannot import name 'ses'`)

- [ ] **Step 3: Implement**

```python
# apps/mail/ses.py
"""The ONE module that calls AWS for mail. Everything else talks to these functions,
so tests stub one seam and nothing else in the app knows boto3 exists.

Credentials come from the ECS task role (deploy/aws/canopy-mail.cfn.yaml).
"""
from __future__ import annotations

from django.conf import settings


class MailOff(Exception):
    """MAIL_ENABLED is false: nothing was sent, created or read."""


def _guard() -> None:
    if not settings.MAIL_ENABLED:
        raise MailOff("mail domains are off on this deployment (MAIL_ENABLED)")


def _sesv2():
    import boto3

    return boto3.client("sesv2", region_name=settings.MAIL_SES_REGION)


def _s3():
    import boto3

    return boto3.client("s3", region_name=settings.MAIL_SES_REGION)


def create_identity(domain: str, *, mail_from: str) -> list[str]:
    _guard()
    out = _sesv2().create_email_identity(
        EmailIdentity=domain, ConfigurationSetName=settings.MAIL_CONFIGURATION_SET)
    _sesv2().put_email_identity_mail_from_attributes(
        EmailIdentity=domain, MailFromDomain=mail_from, BehaviorOnMxFailure="USE_DEFAULT_VALUE")
    return list((out.get("DkimAttributes") or {}).get("Tokens") or [])


def identity_verified(domain: str) -> bool:
    _guard()
    out = _sesv2().get_email_identity(EmailIdentity=domain)
    return (out.get("DkimAttributes") or {}).get("Status") == "SUCCESS"


def delete_identity(domain: str) -> None:
    _guard()
    _sesv2().delete_email_identity(EmailIdentity=domain)


def send_raw(*, from_addr: str, to: list[str], raw: bytes) -> str:
    _guard()
    out = _sesv2().send_email(
        FromEmailAddress=from_addr,
        Destination={"ToAddresses": list(to)},
        Content={"Raw": {"Data": raw}},
        ConfigurationSetName=settings.MAIL_CONFIGURATION_SET,
    )
    return out["MessageId"]


def get_object(key: str) -> bytes:
    _guard()
    return _s3().get_object(Bucket=settings.MAIL_INBOUND_BUCKET, Key=key)["Body"].read()


def keep(key: str) -> str:
    """Copy an `incoming/` object to `kept/` (no lifecycle expiry). Returns the new key."""
    _guard()
    new = "kept/" + key.split("/", 1)[-1]
    _s3().copy_object(Bucket=settings.MAIL_INBOUND_BUCKET, Key=new,
                      CopySource={"Bucket": settings.MAIL_INBOUND_BUCKET, "Key": key})
    return new


def put_object(key: str, data: bytes, content_type: str) -> None:
    _guard()
    _s3().put_object(Bucket=settings.MAIL_INBOUND_BUCKET, Key=key, Body=data,
                     ContentType=content_type or "application/octet-stream")


def presign(key: str, filename: str) -> str:
    _guard()
    return _s3().generate_presigned_url(
        "get_object", ExpiresIn=300,
        Params={"Bucket": settings.MAIL_INBOUND_BUCKET, "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"'})
```

- [ ] **Step 4: Run** `uv run pytest tests/test_mail_ses.py -v` — Expected: PASS
- [ ] **Step 5: Commit** `git add apps/mail/ses.py tests/test_mail_ses.py && git commit -m "feat(mail): SES/S3 seam"`

---

### Task 3: DNS records and live check (`dns.py`)

**Files:**
- Create: `apps/mail/dns.py`
- Test: `tests/test_mail_dns.py`

**Interfaces:**
- Consumes: `MailDomain` (Task 1).
- Produces:
  - `@dataclass DnsRecord(purpose: str, type: str, name: str, value: str, priority: int | None = None, ok: bool | None = None, found: list[str] = [])`
  - `expected_records(domain: MailDomain) -> list[DnsRecord]`
  - `check(domain: MailDomain) -> list[DnsRecord]` (fills `ok`/`found`)
  - `mx_provider(hosts: list[str]) -> str`
  - `merged_spf(existing: str) -> str`
  - Module-level `_resolve(name: str, rtype: str) -> list[str]` (tests patch it).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_dns.py
from __future__ import annotations

import pytest

from apps.mail import dns
from apps.mail.models import MailDomain


def _domain():
    return MailDomain(name="marshellis.com", dkim_tokens=["t1", "t2", "t3"])


def test_spf_is_merged_never_duplicated():
    assert dns.merged_spf("v=spf1 include:zohomail.com -all") == \
        "v=spf1 include:zohomail.com include:amazonses.com -all"
    assert dns.merged_spf("") == "v=spf1 include:amazonses.com -all"
    already = "v=spf1 include:amazonses.com ~all"
    assert dns.merged_spf(already) == already


def test_mx_provider_names_known_hosts():
    assert dns.mx_provider(["mx.zoho.com.", "mx2.zoho.com."]) == "Zoho"
    assert dns.mx_provider(["inbound-smtp.us-east-1.amazonaws.com."]) == "canopy (Amazon SES)"
    assert dns.mx_provider(["mx.example.net."]) == "mx.example.net"
    assert dns.mx_provider([]) == ""


def test_expected_records_cover_dkim_mailfrom_spf_dmarc_mx(monkeypatch):
    monkeypatch.setattr(dns, "_resolve", lambda name, rtype:
                        ["v=spf1 include:zohomail.com -all"] if (name, rtype) == ("marshellis.com", "TXT") else [])
    recs = {r.purpose: r for r in dns.expected_records(_domain())}
    assert recs["dkim-1"].name == "t1._domainkey.marshellis.com"
    assert recs["dkim-1"].value == "t1.dkim.amazonses.com"
    assert recs["mail-from-mx"].value == "feedback-smtp.us-east-1.amazonses.com"
    assert recs["spf"].value == "v=spf1 include:zohomail.com include:amazonses.com -all"
    assert recs["dmarc"].name == "_dmarc.marshellis.com"
    assert recs["mx"].value == "inbound-smtp.us-east-1.amazonaws.com" and recs["mx"].priority == 10


def test_check_marks_each_record(monkeypatch):
    answers = {("t1._domainkey.marshellis.com", "CNAME"): ["t1.dkim.amazonses.com."],
               ("marshellis.com", "MX"): ["10 mx.zoho.com."]}
    monkeypatch.setattr(dns, "_resolve", lambda n, t: answers.get((n, t), []))
    recs = {r.purpose: r for r in dns.check(_domain())}
    assert recs["dkim-1"].ok is True
    assert recs["dkim-2"].ok is False
    assert recs["mx"].ok is False and recs["mx"].found == ["10 mx.zoho.com."]
```

- [ ] **Step 2: Run** `uv run pytest tests/test_mail_dns.py -v` — Expected: FAIL (import)

- [ ] **Step 3: Implement**

```python
# apps/mail/dns.py
"""The records a domain needs, and whether they are published. Read-only DNS:
canopy never writes anyone's zone."""
from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings

_KNOWN_MX = {
    "zoho.com": "Zoho", "zoho.eu": "Zoho", "google.com": "Google", "googlemail.com": "Google",
    "outlook.com": "Microsoft 365", "amazonaws.com": "canopy (Amazon SES)",
    "mailgun.org": "Mailgun", "messagingengine.com": "Fastmail",
}


@dataclass
class DnsRecord:
    purpose: str
    type: str
    name: str
    value: str
    priority: int | None = None
    ok: bool | None = None
    found: list[str] = field(default_factory=list)


def _resolve(name: str, rtype: str) -> list[str]:
    import dns.exception
    import dns.resolver

    try:
        answer = dns.resolver.resolve(name, rtype, lifetime=4)
    except (dns.exception.DNSException, OSError):
        return []
    return [r.to_text().strip('"') if rtype == "TXT" else r.to_text() for r in answer]


def merged_spf(existing: str) -> str:
    if not existing:
        return "v=spf1 include:amazonses.com -all"
    if "include:amazonses.com" in existing:
        return existing
    head, _, tail = existing.rpartition(" ")
    if tail.endswith("all"):
        return f"{head} include:amazonses.com {tail}"
    return f"{existing} include:amazonses.com"


def mx_provider(hosts: list[str]) -> str:
    for h in hosts:
        host = h.split()[-1].rstrip(".").lower()
        for suffix, name in _KNOWN_MX.items():
            if host == suffix or host.endswith("." + suffix):
                return name
    return hosts[0].split()[-1].rstrip(".") if hosts else ""


def expected_records(domain) -> list[DnsRecord]:
    name = domain.name
    region = settings.MAIL_SES_REGION
    mail_from = f"{domain.mail_from_subdomain}.{name}"
    spf_now = next((t for t in _resolve(name, "TXT") if t.startswith("v=spf1")), "")
    recs = [DnsRecord(f"dkim-{i}", "CNAME", f"{tok}._domainkey.{name}", f"{tok}.dkim.amazonses.com")
            for i, tok in enumerate(domain.dkim_tokens, start=1)]
    recs += [
        DnsRecord("mail-from-mx", "MX", mail_from, f"feedback-smtp.{region}.amazonses.com", priority=10),
        DnsRecord("mail-from-spf", "TXT", mail_from, "v=spf1 include:amazonses.com -all"),
        DnsRecord("spf", "TXT", name, merged_spf(spf_now)),
        DnsRecord("dmarc", "TXT", f"_dmarc.{name}", f"v=DMARC1; p=none; rua=mailto:dmarc@{name}"),
        DnsRecord("mx", "MX", name, settings.MAIL_INBOUND_MX, priority=10),
    ]
    return recs


def _matches(rec: DnsRecord, found: list[str]) -> bool:
    want = rec.value.rstrip(".").lower()
    if rec.type == "MX":
        return any(f.split()[-1].rstrip(".").lower() == want for f in found)
    if rec.purpose == "dmarc":
        return any(f.lower().startswith("v=dmarc1") for f in found)
    if rec.purpose in ("spf", "mail-from-spf"):
        return any(f.startswith("v=spf1") and "include:amazonses.com" in f for f in found)
    return any(f.rstrip(".").lower() == want for f in found)


def check(domain) -> list[DnsRecord]:
    out = []
    for rec in expected_records(domain):
        rec.found = _resolve(rec.name, rec.type)
        rec.ok = _matches(rec, rec.found)
        out.append(rec)
    return out
```

- [ ] **Step 4: Keep the suite off live DNS.** In the root `conftest.py` `_no_live_dns` fixture, add (same guarded-import shape as the contacts patch):

```python
    try:
        from apps.mail import dns as mail_dns
    except Exception:
        return
    monkeypatch.setattr(mail_dns, "_resolve", lambda name, rtype: [])
```

Tests that need answers patch `dns._resolve` themselves (they run after the autouse fixture, so theirs wins).

- [ ] **Step 5: Run** `uv run pytest tests/test_mail_dns.py -v` — Expected: PASS
- [ ] **Step 6: Commit** `git add apps/mail/dns.py conftest.py tests/test_mail_dns.py && git commit -m "feat(mail): expected DNS records and live check"`

---

### Task 4: Domain + route services and the admin REST surface

**Files:**
- Create: `apps/mail/domains.py`, `apps/mail/schemas.py`, `apps/mail/api.py`, `apps/mail/management/__init__.py`, `apps/mail/management/commands/__init__.py`, `apps/mail/management/commands/mail_refresh.py`
- Modify: `apps/api/api.py` (import + `api.add_router("/mail", mail_router)` beside inbound, ~line 207/236), `apps/api/route_gates.py` (gates + `SETTINGS` + `NOT_SETTINGS`), `frontend/src/api/generated.ts` (regenerated)
- Test: `tests/test_mail_domains_api.py`

**Interfaces:**
- Consumes: `ses.create_identity/identity_verified/delete_identity`, `dns.check/mx_provider/_resolve`.
- Produces (`domains.py`):
  - `class DomainError(Exception)` with `.status: int`
  - `add_domain(workspace, name: str, *, by) -> MailDomain`
  - `refresh(domain: MailDomain, *, force: bool = False) -> MailDomain` (skips if `checked_at` < 60 s ago unless `force`)
  - `remove_domain(domain: MailDomain) -> None`
  - `save_route(domain, *, local_part: str, kind: str, agent_slug: str = "", forward_to_user_id: int | None = None, enabled: bool = True) -> MailRoute`
  - `resolve(address: str) -> tuple[MailDomain | None, MailRoute | None]` — subdomains resolve to the verified parent; exact local part beats `*`; disabled routes and non-live domains return `(domain, None)`.
- REST (router `/api/mail`, tag `mail`): `GET /domains/{workspace}` (`list_mail_domains`, member), `POST /domains/{workspace}` (`add_mail_domain`, integrations), `POST /domains/{workspace}/{domain_id}/refresh` (`refresh_mail_domain`, member — action), `DELETE /domains/{workspace}/{domain_id}` (`remove_mail_domain`, integrations), `PUT /domains/{workspace}/{domain_id}/routes` (`save_mail_route`, integrations), `DELETE /domains/{workspace}/{domain_id}/routes/{route_id}` (`delete_mail_route`, integrations), `PATCH /domains/{workspace}/{domain_id}` (`update_mail_domain`, integrations — `daily_send_cap`, `unsuspend: bool`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_domains_api.py
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.mail import domains, ses
from apps.mail.models import MailDomain, MailRoute
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def stub_aws(monkeypatch, settings):
    settings.MAIL_ENABLED = True
    monkeypatch.setattr(ses, "create_identity", lambda d, mail_from: ["t1", "t2", "t3"])
    monkeypatch.setattr(ses, "identity_verified", lambda d: True)
    monkeypatch.setattr(ses, "delete_identity", lambda d: None)
    from apps.mail import dns
    monkeypatch.setattr(dns, "_resolve", lambda n, t: ["10 mx.zoho.com."] if t == "MX" else [])


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jjackson@dimagi.com", "pw")


@pytest.fixture()
def ws(owner):
    w = Workspace.objects.create(slug="marshellis", display_name="M", created_by=owner)
    WorkspaceMembership.objects.create(workspace=w, user=owner, role="owner")
    return w


def _c(u):
    c = Client(); c.force_login(u); return c


def test_admin_adds_a_domain_and_sees_records_and_current_provider(owner, ws):
    r = _c(owner).post("/api/mail/domains/marshellis", {"name": "Marshellis.com"},
                       content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["name"] == "marshellis.com"
    assert body["mx_provider"] == "Zoho"
    assert {rec["purpose"] for rec in body["records"]} >= {"dkim-1", "spf", "dmarc", "mx"}


def test_editor_cannot_add_a_domain(ws):
    ed = User.objects.create_user("ed", "ed@x.com", "pw")
    WorkspaceMembership.objects.create(workspace=ws, user=ed, role="editor")
    r = _c(ed).post("/api/mail/domains/marshellis", {"name": "marshellis.com"},
                    content_type="application/json")
    assert r.status_code == 403


def test_domain_taken_elsewhere_is_409_without_naming_the_holder(owner, ws):
    other = Workspace.objects.create(slug="o", display_name="O", created_by=owner)
    MailDomain.objects.create(workspace=other, name="marshellis.com")
    r = _c(owner).post("/api/mail/domains/marshellis", {"name": "marshellis.com"},
                       content_type="application/json")
    assert r.status_code == 409 and "o" not in r.json().get("detail", "").split()


def test_forward_route_requires_a_member(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    stranger = User.objects.create_user("s", "s@x.com", "pw")
    r = _c(owner).put(f"/api/mail/domains/marshellis/{d.pk}/routes",
                      {"local_part": "jjackson", "kind": "forward", "forward_to_user_id": stranger.pk},
                      content_type="application/json")
    assert r.status_code == 422
    ok = _c(owner).put(f"/api/mail/domains/marshellis/{d.pk}/routes",
                       {"local_part": "jjackson", "kind": "forward", "forward_to_user_id": owner.pk},
                       content_type="application/json")
    assert ok.status_code == 200, ok.content


def test_reserved_local_part_cannot_be_routed(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    r = _c(owner).put(f"/api/mail/domains/marshellis/{d.pk}/routes",
                      {"local_part": "canopy-forwarder", "kind": "drop"}, content_type="application/json")
    assert r.status_code == 422


def test_resolve_exact_beats_catch_all_and_subdomain_uses_parent(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws)
    MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=owner)
    assert domains.resolve("Ada@marshellis.com")[1].kind == "agent"
    assert domains.resolve("apple@marshellis.com")[1].kind == "forward"
    assert domains.resolve("ada@mail.marshellis.com")[1].kind == "agent"
    assert domains.resolve("x@nobody.org") == (None, None)


def test_pending_domain_routes_nothing(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="pending")
    MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=owner)
    assert domains.resolve("a@marshellis.com") == (d, None)


def test_refresh_flips_pending_to_verified(owner, ws):
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", dkim_tokens=["t1"])
    assert domains.refresh(d, force=True).status == "verified"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_mail_domains_api.py -v` — Expected: FAIL (import)

- [ ] **Step 3: Implement `domains.py`**

```python
# apps/mail/domains.py
"""Domains and routes: what an admin configures, and the address → route answer."""
from __future__ import annotations

import datetime as dt
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.agents.models import Agent
from apps.workspaces import services as wsvc

from . import dns, ses
from .models import MailDomain, MailRoute

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LOCAL_RE = re.compile(r"^(\*|[a-z0-9!#$%&'+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'+/=?^_`{|}~-]+)*)$")


class DomainError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def add_domain(workspace, name: str, *, by) -> MailDomain:
    name = (name or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(name):
        raise DomainError(422, "that is not a domain name")
    if MailDomain.objects.filter(name=name).exists():
        raise DomainError(409, "that domain is already registered")
    try:
        with transaction.atomic():
            d = MailDomain.objects.create(workspace=workspace, name=name, created_by=by)
    except IntegrityError:
        raise DomainError(409, "that domain is already registered") from None
    try:
        d.dkim_tokens = ses.create_identity(name, mail_from=f"{d.mail_from_subdomain}.{name}")
    except ses.MailOff as exc:
        d.delete()
        raise DomainError(503, str(exc)) from None
    d.save(update_fields=["dkim_tokens"])
    return refresh(d, force=True)


def refresh(domain: MailDomain, *, force: bool = False) -> MailDomain:
    now = timezone.now()
    if not force and domain.checked_at and now - domain.checked_at < dt.timedelta(seconds=60):
        return domain
    if domain.status == MailDomain.PENDING:
        try:
            if ses.identity_verified(domain.name):
                domain.status = MailDomain.VERIFIED
        except ses.MailOff:
            pass
    mx = dns._resolve(domain.name, "MX")
    domain.mx_provider = dns.mx_provider(mx)
    domain.receiving = any(settings.MAIL_INBOUND_MX in h.lower() for h in mx)
    domain.checked_at = now
    domain.save(update_fields=["status", "mx_provider", "receiving", "checked_at"])
    return domain


def remove_domain(domain: MailDomain) -> None:
    try:
        ses.delete_identity(domain.name)
    except ses.MailOff:
        pass
    domain.routes.update(enabled=False)
    domain.delete()


def save_route(domain: MailDomain, *, local_part: str, kind: str, agent_slug: str = "",
               forward_to_user_id: int | None = None, enabled: bool = True) -> MailRoute:
    local = (local_part or "").strip().lower()
    if not _LOCAL_RE.match(local):
        raise DomainError(422, "that is not a valid address before the @")
    if local == settings.MAIL_FORWARDER_LOCAL_PART:
        raise DomainError(422, f"{local}@ is reserved for forwarded mail")
    agent = forward_to = None
    if kind == MailRoute.AGENT:
        agent = Agent.objects.filter(slug=agent_slug, workspace=domain.workspace).first()
        if agent is None:
            raise DomainError(422, f"no agent {agent_slug!r} in this workspace")
    elif kind == MailRoute.FORWARD:
        forward_to = get_user_model().objects.filter(pk=forward_to_user_id).first()
        if forward_to is None or not wsvc.is_member(forward_to, domain.workspace):
            raise DomainError(422, "mail can only be forwarded to a member of this workspace")
    elif kind != MailRoute.DROP:
        raise DomainError(422, "kind must be agent, forward or drop")
    route, _ = MailRoute.objects.update_or_create(
        domain=domain, local_part=local,
        defaults={"kind": kind, "agent": agent, "forward_to": forward_to, "enabled": enabled})
    return route


def _domain_for(host: str) -> MailDomain | None:
    host = host.lower().rstrip(".")
    labels = host.split(".")
    candidates = [".".join(labels[i:]) for i in range(len(labels) - 1)]
    found = {d.name: d for d in MailDomain.objects.filter(name__in=candidates).select_related("workspace")}
    return next((found[c] for c in candidates if c in found), None)


def resolve(address: str) -> tuple[MailDomain | None, MailRoute | None]:
    local, _, host = (address or "").strip().lower().rpartition("@")
    if not local or not host:
        return None, None
    domain = _domain_for(host)
    if domain is None:
        return None, None
    if not domain.routes_live:
        return domain, None
    routes = {r.local_part: r for r in domain.routes.filter(
        local_part__in=[local, MailRoute.CATCH_ALL], enabled=True).select_related("agent", "forward_to")}
    return domain, routes.get(local) or routes.get(MailRoute.CATCH_ALL)
```

(Check `apps/workspaces/services.py` for the exact membership predicate name — CLAUDE.md names `member_role`/`is_member`/`membership`; use `is_member(user, workspace)` with whatever argument form it takes.)

- [ ] **Step 4: Implement `schemas.py` + `api.py`**

```python
# apps/mail/schemas.py
from __future__ import annotations

from ninja import Schema


class DnsRecordOut(Schema):
    purpose: str
    type: str
    name: str
    value: str
    priority: int | None = None
    ok: bool | None = None
    found: list[str] = []


class MailRouteOut(Schema):
    id: int
    local_part: str
    address: str
    kind: str
    agent_slug: str = ""
    forward_to_user_id: int | None = None
    forward_to_email: str = ""
    enabled: bool


class MailDomainOut(Schema):
    id: int
    name: str
    status: str
    receiving: bool
    mx_provider: str
    suspended_reason: str
    daily_send_cap: int
    records: list[DnsRecordOut]
    routes: list[MailRouteOut]
    can_manage: bool


class MailDomainListOut(Schema):
    enabled: bool
    items: list[MailDomainOut]


class MailDomainIn(Schema):
    name: str


class MailDomainPatchIn(Schema):
    daily_send_cap: int | None = None
    unsuspend: bool = False


class MailRouteIn(Schema):
    local_part: str
    kind: str
    agent_slug: str = ""
    forward_to_user_id: int | None = None
    enabled: bool = True
```

```python
# apps/mail/api.py
"""Django Ninja router for /api/mail — domains, routes, agent mail, and the SNS doors.

The workspace is in the path, the `apps/inbound` idiom. Domain and route writes
are workspace settings (INTEGRATIONS: admin or owner), audited via route_gates.SETTINGS.
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.events import audit
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import dns, domains
from .models import MailDomain, MailRoute
from .schemas import (MailDomainIn, MailDomainListOut, MailDomainOut, MailDomainPatchIn,
                      MailRouteIn, MailRouteOut)

router = Router(auth=session_auth, tags=["mail"])


def _ws(user, slug: str) -> Workspace:
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or slug not in wsvc.user_workspace_slugs(user):
        raise HttpError(404, "no such workspace")
    return ws


def _admin_ws(user, slug: str) -> Workspace:
    ws = _ws(user, slug)
    if not perms.can(user, ws, perms.INTEGRATIONS):
        raise HttpError(403, "requires the admin role or above")
    return ws


def _domain(ws, domain_id: int) -> MailDomain:
    d = MailDomain.objects.filter(pk=domain_id, workspace=ws).first()
    if d is None:
        raise HttpError(404, "no such domain")
    return d


def _route_out(r: MailRoute) -> dict:
    return {"id": r.pk, "local_part": r.local_part, "address": r.address, "kind": r.kind,
            "agent_slug": r.agent.slug if r.agent_id else "",
            "forward_to_user_id": r.forward_to_id,
            "forward_to_email": r.forward_to.email if r.forward_to_id else "",
            "enabled": r.enabled}


def _domain_out(d: MailDomain, user) -> dict:
    return {"id": d.pk, "name": d.name, "status": d.status, "receiving": d.receiving,
            "mx_provider": d.mx_provider, "suspended_reason": d.suspended_reason,
            "daily_send_cap": d.daily_send_cap,
            "records": [vars(r) for r in dns.check(d)],
            "routes": [_route_out(r) for r in d.routes.select_related("agent", "forward_to")],
            "can_manage": perms.can(user, d.workspace, perms.INTEGRATIONS)}


def _raise(exc: domains.DomainError):
    raise HttpError(exc.status, str(exc))


@router.get("/domains/{workspace}", response=MailDomainListOut, summary="List mail domains")
def list_mail_domains(request: HttpRequest, workspace: str) -> dict:
    ws = _ws(request.user, workspace)
    items = [_domain_out(domains.refresh(d), request.user) for d in ws.mail_domains.all()]
    return {"enabled": settings.MAIL_ENABLED, "items": items}


@router.post("/domains/{workspace}", response=MailDomainOut, summary="Add a mail domain (admin or owner)")
def add_mail_domain(request: HttpRequest, workspace: str, payload: MailDomainIn) -> dict:
    ws = _admin_ws(request.user, workspace)
    try:
        d = domains.add_domain(ws, payload.name, by=request.user)
    except domains.DomainError as exc:
        _raise(exc)
    audit.note_change(scope=f"mail.domain:{d.name}", before=None, after={"name": d.name})
    return _domain_out(d, request.user)


@router.post("/domains/{workspace}/{domain_id}/refresh", response=MailDomainOut,
             summary="Re-check a domain's DNS and verification now")
def refresh_mail_domain(request: HttpRequest, workspace: str, domain_id: int) -> dict:
    ws = _ws(request.user, workspace)
    return _domain_out(domains.refresh(_domain(ws, domain_id), force=True), request.user)


@router.patch("/domains/{workspace}/{domain_id}", response=MailDomainOut,
              summary="Change a domain's send cap or lift its suspension (admin or owner)")
def update_mail_domain(request: HttpRequest, workspace: str, domain_id: int,
                       payload: MailDomainPatchIn) -> dict:
    ws = _admin_ws(request.user, workspace)
    d = _domain(ws, domain_id)
    before = {"daily_send_cap": d.daily_send_cap, "status": d.status}
    if payload.daily_send_cap is not None:
        if not 1 <= payload.daily_send_cap <= 5000:
            raise HttpError(422, "daily_send_cap must be between 1 and 5000")
        d.daily_send_cap = payload.daily_send_cap
    if payload.unsuspend and d.status == MailDomain.SUSPENDED:
        d.status, d.suspended_reason = MailDomain.VERIFIED, ""
    d.save()
    audit.note_change(scope=f"mail.domain:{d.name}", before=before,
                      after={"daily_send_cap": d.daily_send_cap, "status": d.status})
    return _domain_out(d, request.user)


@router.delete("/domains/{workspace}/{domain_id}", response={204: None},
               summary="Remove a mail domain (admin or owner)")
def remove_mail_domain(request: HttpRequest, workspace: str, domain_id: int):
    ws = _admin_ws(request.user, workspace)
    d = _domain(ws, domain_id)
    audit.note_change(scope=f"mail.domain:{d.name}", before={"name": d.name}, after=None)
    domains.remove_domain(d)
    return 204, None


@router.put("/domains/{workspace}/{domain_id}/routes", response=MailRouteOut,
            summary="Create or replace the route for one address (admin or owner)")
def save_mail_route(request: HttpRequest, workspace: str, domain_id: int, payload: MailRouteIn) -> dict:
    ws = _admin_ws(request.user, workspace)
    d = _domain(ws, domain_id)
    prior = d.routes.filter(local_part=payload.local_part.strip().lower()).first()
    try:
        r = domains.save_route(d, local_part=payload.local_part, kind=payload.kind,
                               agent_slug=payload.agent_slug,
                               forward_to_user_id=payload.forward_to_user_id, enabled=payload.enabled)
    except domains.DomainError as exc:
        _raise(exc)
    audit.note_change(scope=f"mail.route:{r.address}",
                      before=_route_out(prior) if prior else None, after=_route_out(r))
    return _route_out(r)


@router.delete("/domains/{workspace}/{domain_id}/routes/{route_id}", response={204: None},
               summary="Remove an address's route (admin or owner)")
def delete_mail_route(request: HttpRequest, workspace: str, domain_id: int, route_id: int):
    ws = _admin_ws(request.user, workspace)
    r = _domain(ws, domain_id).routes.filter(pk=route_id).first()
    if r is None:
        raise HttpError(404, "no such route")
    audit.note_change(scope=f"mail.route:{r.address}", before=_route_out(r), after=None)
    r.delete()
    return 204, None
```

```python
# apps/mail/management/commands/mail_refresh.py
from django.core.management.base import BaseCommand

from apps.mail import domains
from apps.mail.models import MailDomain


class Command(BaseCommand):
    help = "Re-check every mail domain's SES verification and MX now."

    def handle(self, *args, **opts):
        for d in MailDomain.objects.all():
            d = domains.refresh(d, force=True)
            self.stdout.write(f"{d.name}: {d.status} receiving={d.receiving} mx={d.mx_provider}")
```

Wire up: in `apps/api/api.py` add `from apps.mail.api import router as mail_router  # noqa: E402` next to the inbound import and `api.add_router("/mail", mail_router)` next to `add_router("/inbound", …)`.

route_gates (`apps/api/route_gates.py`), add a block after `# --- apps/inbound/api.py`:

```python
    # --- apps/mail/api.py
    "list_mail_domains": ("member",),
    "add_mail_domain": ("integrations",),
    "refresh_mail_domain": ("member",),  # re-reads DNS/SES; changes no configuration
    "update_mail_domain": ("integrations",),
    "remove_mail_domain": ("integrations",),
    "save_mail_route": ("integrations",),
    "delete_mail_route": ("integrations",),
```

Add `"add_mail_domain", "update_mail_domain", "remove_mail_domain", "save_mail_route", "delete_mail_route",` to `SETTINGS`; add `"refresh_mail_domain": "action",` to `NOT_SETTINGS`. If `test_every_route_declares_its_gate` requires member-level POSTs in `VIEWER_MAY_MUTATE`, add `"refresh_mail_domain": "re-checks DNS; writes only cached status"`.

- [ ] **Step 5: Run** `uv run pytest tests/test_mail_domains_api.py tests/test_every_route_declares_its_gate.py tests/test_roles_named_only_in_workspaces.py -v` — Expected: PASS
- [ ] **Step 6: Regenerate types** `cd frontend && npm run gen:api:local && cd ..`
- [ ] **Step 7: Commit** `git add apps/mail apps/api frontend/src/api/generated.ts tests/test_mail_domains_api.py && git commit -m "feat(mail): domains and routes — services and admin REST"`

---

### Task 5: Parsing (`parse.py`) and threading (`threads.py`)

**Files:**
- Create: `apps/mail/parse.py`, `apps/mail/threads.py`, `tests/fixtures/mail/` (`plain.eml`, `html_only.eml`, `reply.eml`, `encoded_headers.eml`, `with_attachment.eml`, `no_message_id.eml`)
- Test: `tests/test_mail_parse.py`

**Interfaces:**
- Produces:
  - `@dataclass Attachment(filename: str, content_type: str, data: bytes)`
  - `@dataclass Parsed(message_id, in_reply_to, references: list[str], from_addr, from_name, to: list[str], cc: list[str], subject, text, html, attachments: list[Attachment], headers: list[tuple[str, str]], automated: bool)`
  - `parse(raw: bytes) -> Parsed`
  - `threads.place(route: MailRoute, parsed: Parsed) -> MailThread` (existing thread whose messages' `message_id` ∈ `{in_reply_to} ∪ references`, scoped to the route; else a new thread)

- [ ] **Step 1: Write fixtures** (each a literal RFC 5322 message). Example `tests/fixtures/mail/encoded_headers.eml`:

```
From: =?utf-8?q?Jos=C3=A9_P=C3=A9rez?= <jose@example.org>
To: ada@marshellis.com
Subject: =?utf-8?b?wqFIb2xhIQ==?=
Message-ID: <enc-1@example.org>
Date: Mon, 12 Oct 2026 10:00:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset=iso-8859-1
Content-Transfer-Encoding: quoted-printable

Ma=F1ana
```

`no_message_id.eml` — same shape, no `Message-ID:`. `html_only.eml` — `Content-Type: text/html` body `<p>Hello <b>there</b></p>`. `reply.eml` — `In-Reply-To: <orig-1@marshellis.com>` and `References: <orig-0@x> <orig-1@marshellis.com>`. `with_attachment.eml` — `multipart/mixed` with a text part and a base64 `application/pdf` part `filename="a.pdf"`. `plain.eml` — plain text plus `Auto-Submitted: auto-replied` for the automated check (call it `auto.eml`; keep `plain.eml` without it).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_mail_parse.py
from __future__ import annotations

from pathlib import Path

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.mail import parse, threads
from apps.mail.models import MailDomain, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace

FIX = Path(__file__).parent / "fixtures" / "mail"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_decodes_rfc2047_headers_and_latin1_body():
    p = parse.parse(raw("encoded_headers.eml"))
    assert p.from_name == "José Pérez" and p.from_addr == "jose@example.org"
    assert p.subject == "¡Hola!"
    assert p.text.strip() == "Mañana"


def test_missing_message_id_gets_a_stable_synthetic_one():
    a = parse.parse(raw("no_message_id.eml"))
    b = parse.parse(raw("no_message_id.eml"))
    assert a.message_id == b.message_id and a.message_id.endswith("@canopy>")


def test_html_only_mail_gets_a_text_rendering():
    p = parse.parse(raw("html_only.eml"))
    assert "Hello there" in p.text and "<b>" in p.html


def test_attachment_is_extracted():
    p = parse.parse(raw("with_attachment.eml"))
    assert [(a.filename, a.content_type) for a in p.attachments] == [("a.pdf", "application/pdf")]


def test_automated_markers():
    assert parse.parse(raw("auto.eml")).automated is True
    assert parse.parse(raw("plain.eml")).automated is False


@pytest.mark.django_db
def test_reply_joins_the_thread_it_references():
    u = User.objects.create_user("jj", "jj@x.com", "pw")
    ws = Workspace.objects.create(slug="m", display_name="M", created_by=u)
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws)
    r = MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    t = MailThread.objects.create(workspace=ws, route=r, subject="x")
    MailMessage.objects.create(thread=t, direction="out", message_id="<orig-1@marshellis.com>")
    assert threads.place(r, parse.parse(raw("reply.eml"))) == t
    assert threads.place(r, parse.parse(raw("plain.eml"))) != t
```

- [ ] **Step 3: Run** `uv run pytest tests/test_mail_parse.py -v` — Expected: FAIL (import)

- [ ] **Step 4: Implement**

```python
# apps/mail/parse.py
"""Raw MIME → the fields canopy stores. stdlib only."""
from __future__ import annotations

import hashlib
import html as htmlmod
import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class Parsed:
    message_id: str
    in_reply_to: str
    references: list[str]
    from_addr: str
    from_name: str
    to: list[str]
    cc: list[str]
    subject: str
    text: str
    html: str
    attachments: list[Attachment] = field(default_factory=list)
    headers: list[tuple[str, str]] = field(default_factory=list)
    automated: bool = False


_IDS = re.compile(r"<[^<>\s]+>")
_TAGS = re.compile(r"<[^>]+>")


def _addrs(values) -> list[str]:
    return [a.lower() for _, a in getaddresses([str(v) for v in values or []]) if a]


def _html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>", "\n", h)
    return re.sub(r"[ \t]+", " ", htmlmod.unescape(_TAGS.sub("", h))).strip()


def _is_automated(msg) -> bool:
    auto = str(msg.get("Auto-Submitted", "")).strip().lower()
    prec = str(msg.get("Precedence", "")).strip().lower()
    return bool((auto and auto != "no") or prec in ("bulk", "list", "junk")
                or msg.get("List-Unsubscribe"))


def parse(raw: bytes) -> Parsed:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    name, addr = parseaddr(str(msg.get("From", "")))
    message_id = str(msg.get("Message-ID", "")).strip()
    if not _IDS.fullmatch(message_id or ""):
        message_id = f"<{hashlib.sha256(raw).hexdigest()[:32]}@canopy>"
    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    html = html_part.get_content() if html_part is not None else ""
    text = text_part.get_content() if text_part is not None else _html_to_text(html)
    attachments = [
        Attachment(part.get_filename() or "attachment", part.get_content_type(),
                   part.get_payload(decode=True) or b"")
        for part in msg.iter_attachments()
    ]
    return Parsed(
        message_id=message_id,
        in_reply_to=" ".join(_IDS.findall(str(msg.get("In-Reply-To", "")))),
        references=_IDS.findall(str(msg.get("References", ""))),
        from_addr=addr.lower(), from_name=name,
        to=_addrs(msg.get_all("To")), cc=_addrs(msg.get_all("Cc")),
        subject=str(msg.get("Subject", "")), text=text, html=html,
        attachments=attachments,
        headers=[(k, str(v)) for k, v in msg.items()],
        automated=_is_automated(msg),
    )
```

```python
# apps/mail/threads.py
"""Which conversation a message belongs to: RFC 5322 reply headers, scoped to one route."""
from __future__ import annotations

from .models import MailMessage, MailRoute, MailThread


def place(route: MailRoute, parsed) -> MailThread:
    ids = set(parsed.references) | ({parsed.in_reply_to} if parsed.in_reply_to else set())
    if ids:
        hit = (MailMessage.objects.filter(thread__route=route, message_id__in=ids)
               .select_related("thread").order_by("-created_at").first())
        if hit is not None:
            return hit.thread
    return MailThread.objects.create(workspace=route.domain.workspace, route=route,
                                     subject=parsed.subject[:998])
```

- [ ] **Step 5: Run** `uv run pytest tests/test_mail_parse.py -v` — Expected: PASS
- [ ] **Step 6: Commit** `git add apps/mail/parse.py apps/mail/threads.py tests/fixtures/mail tests/test_mail_parse.py && git commit -m "feat(mail): MIME parsing and reply threading"`

---

### Task 6: SNS verification + inbound endpoint (`sns.py`, `inbound.py`)

**Files:**
- Create: `apps/mail/sns.py`, `apps/mail/inbound.py`
- Modify: `apps/mail/api.py` (add `ses_inbound` route), `apps/common/middleware.py` (`PUBLIC_PATH_PREFIXES` += `"/api/mail/ses/"` with comment), `apps/api/route_gates.py` (`"ses_inbound": ("host",)`, `NOT_SETTINGS["ses_inbound"] = "telemetry"` — or the closest existing category for an inbound webhook; `gmail_push` shows which it uses), `pyproject.toml` (declare `"cryptography>=42"` — already in the lock transitively)
- Test: `tests/test_mail_inbound.py`

**Interfaces:**
- Consumes: `parse.parse`, `domains.resolve`, `ses.get_object`.
- Produces:
  - `sns.verify(msg: dict, *, topic_arn: str) -> None` raises `sns.Invalid`; `sns.confirm(msg: dict) -> None`; module-level `_fetch_cert(url) -> bytes` and `_get(url) -> None` (tests patch).
  - `inbound.handle(notification: dict) -> list[dict]` — one `{"recipient", "outcome"}` per recipient, outcomes `agent` | `forwarded` | `dropped:<reason>` | `duplicate`.
  - Hooks (filled by Tasks 7/8): `inbound.deliver_agent(route, parsed, raw_key) -> str` and `inbound.deliver_forward(route, parsed, raw, recipient) -> str` — Task 6 defines them as functions that later tasks implement in `agent_mail.py` / `forward.py`; for this task stub them inside the test via monkeypatch.
- REST: `POST /api/mail/ses/inbound/` (`ses_inbound`, `auth=None`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_inbound.py
from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.mail import inbound, ses, sns
from apps.mail.models import MailDomain, MailRoute
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
FIX = Path(__file__).parent / "fixtures" / "mail"
TOPIC = "arn:aws:sns:us-east-1:858923557655:canopy-inbound-mail"


@pytest.fixture()
def world(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    settings.MAIL_INBOUND_TOPIC_ARN = TOPIC
    u = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=u)
    WorkspaceMembership.objects.create(workspace=ws, user=u, role="owner")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws)
    MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=u)
    calls = []
    monkeypatch.setattr(inbound, "deliver_agent", lambda r, p, k: calls.append(("agent", r.address)) or "agent")
    monkeypatch.setattr(inbound, "deliver_forward", lambda r, p, raw, rcpt: calls.append(("fwd", rcpt)) or "forwarded")
    monkeypatch.setattr(ses, "get_object", lambda key: (FIX / "plain.eml").read_bytes())
    return type("W", (), {"calls": calls, "user": u})


def note(recipients, verdict="PASS"):
    return {"receipt": {"recipients": recipients, "spamVerdict": {"status": verdict},
                        "virusVerdict": {"status": "PASS"},
                        "action": {"type": "S3", "objectKey": "incoming/abc"}},
            "mail": {"messageId": "ses-abc"}}


def test_each_recipient_routes_once(world):
    out = inbound.handle(note(["ada@marshellis.com", "apple@marshellis.com", "x@other.org"]))
    assert [o["outcome"] for o in out] == ["agent", "forwarded", "dropped:unknown-domain"]


def test_spam_is_dropped_for_everyone(world):
    out = inbound.handle(note(["ada@marshellis.com"], verdict="FAIL"))
    assert out[0]["outcome"] == "dropped:spam" and world.calls == []


def test_unsigned_post_is_404(world):
    r = Client().post("/api/mail/ses/inbound/", json.dumps({"Type": "Notification"}),
                      content_type="text/plain; charset=UTF-8")
    assert r.status_code == 404


def test_signed_notification_is_handled(world, monkeypatch):
    monkeypatch.setattr(sns, "verify", lambda m, topic_arn: None)
    body = {"Type": "Notification", "TopicArn": TOPIC,
            "Message": json.dumps(note(["ada@marshellis.com"]))}
    r = Client().post("/api/mail/ses/inbound/", json.dumps(body), content_type="text/plain; charset=UTF-8")
    assert r.status_code == 200 and world.calls == [("agent", "ada@marshellis.com")]


def test_sns_rejects_a_foreign_cert_host():
    with pytest.raises(sns.Invalid):
        sns.verify({"Type": "Notification", "TopicArn": TOPIC, "SignatureVersion": "2",
                    "SigningCertURL": "https://evil.example/cert.pem", "Signature": "x"},
                   topic_arn=TOPIC)


def test_sns_rejects_the_wrong_topic():
    with pytest.raises(sns.Invalid):
        sns.verify({"Type": "Notification", "TopicArn": "arn:aws:sns:us-east-1:1:other"},
                   topic_arn=TOPIC)
```

Add one more test that signs a message with a throwaway RSA key (generated in the test with `cryptography`), patches `sns._fetch_cert` to return its self-signed PEM, and asserts `verify` passes for SignatureVersion 2 (SHA256) and fails after mutating `Message`.

- [ ] **Step 2: Run** `uv run pytest tests/test_mail_inbound.py -v` — Expected: FAIL (import)

- [ ] **Step 3: Implement `sns.py`**

```python
# apps/mail/sns.py
"""SNS HTTPS-delivery verification. Verified BEFORE any field is trusted.

https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
"""
from __future__ import annotations

import base64
import re
import urllib.request
from functools import lru_cache

_CERT_HOST = re.compile(r"^https://sns\.[a-z0-9-]+\.amazonaws\.com/[^?#]+\.pem$")
_FIELDS = {
    "Notification": ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"],
    "SubscriptionConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token",
                                 "TopicArn", "Type"],
    "UnsubscribeConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token",
                                "TopicArn", "Type"],
}


class Invalid(Exception):
    pass


@lru_cache(maxsize=8)
def _fetch_cert(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 — host pinned by _CERT_HOST
        return r.read()


def _get(url: str) -> None:
    with urllib.request.urlopen(url, timeout=5):  # noqa: S310
        pass


def _string_to_sign(msg: dict) -> bytes:
    keys = _FIELDS.get(msg.get("Type", ""))
    if keys is None:
        raise Invalid("unknown SNS message type")
    return "".join(f"{k}\n{msg[k]}\n" for k in keys if k in msg).encode()


def verify(msg: dict, *, topic_arn: str) -> None:
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    if not topic_arn or msg.get("TopicArn") != topic_arn:
        raise Invalid("not our topic")
    url = msg.get("SigningCertURL", "")
    if not _CERT_HOST.match(url):
        raise Invalid("signing cert is not hosted by SNS")
    algo = {"1": hashes.SHA1(), "2": hashes.SHA256()}.get(str(msg.get("SignatureVersion")))
    if algo is None:
        raise Invalid("unsupported signature version")
    cert = x509.load_pem_x509_certificate(_fetch_cert(url))
    try:
        cert.public_key().verify(base64.b64decode(msg.get("Signature", "")),
                                 _string_to_sign(msg), padding.PKCS1v15(), algo)
    except (InvalidSignature, ValueError) as exc:
        raise Invalid("bad signature") from exc


def confirm(msg: dict) -> None:
    url = msg.get("SubscribeURL", "")
    if not re.match(r"^https://sns\.[a-z0-9-]+\.amazonaws\.com/", url):
        raise Invalid("subscribe URL is not SNS")
    _get(url)
```

- [ ] **Step 4: Implement `inbound.py`**

```python
# apps/mail/inbound.py
"""One SES receipt notification → one outcome per recipient.

Order matters: verdicts first (spam/virus never reach a person or an agent),
then each recipient independently through domains.resolve.
"""
from __future__ import annotations

import logging

from . import domains, parse, ses
from .models import MailRoute

logger = logging.getLogger(__name__)


def deliver_agent(route, parsed, raw_key: str) -> str:
    from . import agent_mail

    return agent_mail.deliver(route, parsed, raw_key)


def deliver_forward(route, parsed, raw: bytes, recipient: str) -> str:
    from . import forward

    return forward.forward(route, parsed, raw, recipient)


def handle(notification: dict) -> list[dict]:
    receipt = notification.get("receipt") or {}
    recipients = [r.lower() for r in receipt.get("recipients") or []]
    bad = [k for k in ("spamVerdict", "virusVerdict")
           if (receipt.get(k) or {}).get("status") == "FAIL"]
    if bad:
        reason = "spam" if "spamVerdict" in bad else "virus"
        logger.info("mail dropped (%s) for %s", reason, recipients)
        return [{"recipient": r, "outcome": f"dropped:{reason}"} for r in recipients]
    key = (receipt.get("action") or {}).get("objectKey", "")
    raw = ses.get_object(key)
    parsed = parse.parse(raw)
    out = []
    for rcpt in recipients:
        domain, route = domains.resolve(rcpt)
        if domain is None:
            outcome = "dropped:unknown-domain"
        elif route is None:
            outcome = "dropped:no-route"
        elif route.kind == MailRoute.AGENT:
            outcome = deliver_agent(route, parsed, key)
        elif route.kind == MailRoute.FORWARD:
            outcome = deliver_forward(route, parsed, raw, rcpt)
        else:
            outcome = "dropped:drop-route"
        out.append({"recipient": rcpt, "outcome": outcome})
    logger.info("mail %s: %s", parsed.message_id, out)
    return out
```

- [ ] **Step 5: Add the route** to `apps/mail/api.py`:

```python
import json

from . import inbound, sns


@router.post("/ses/inbound/", auth=None, response={200: dict, 404: dict, 500: dict},
             summary="SES receipt notifications (SNS HTTPS delivery)")
def ses_inbound(request: HttpRequest):
    # Verify before trusting a byte; 404 on any failure so a probe learns nothing.
    try:
        msg = json.loads(request.body or b"{}")
        sns.verify(msg, topic_arn=settings.MAIL_INBOUND_TOPIC_ARN)
    except (ValueError, sns.Invalid):
        return 404, {"detail": "not found"}
    if msg.get("Type") == "SubscriptionConfirmation":
        sns.confirm(msg)
        return 200, {"confirmed": True}
    if msg.get("Type") != "Notification":
        return 200, {"ignored": msg.get("Type")}
    try:
        out = inbound.handle(json.loads(msg.get("Message") or "{}"))
    except Exception:  # noqa: BLE001 — transient (S3/SES): let SNS retry
        logger.exception("mail inbound failed; SNS will retry")
        return 500, {"detail": "retry"}
    return 200, {"outcomes": out}
```

(Add `import logging` / `logger = logging.getLogger(__name__)` at module top.) SNS posts `Content-Type: text/plain`, which is why the view reads `request.body` instead of a schema.

- [ ] **Step 6: Run** `uv run pytest tests/test_mail_inbound.py tests/test_every_route_declares_its_gate.py -v` — Expected: PASS
- [ ] **Step 7: Commit** `git add -A apps/mail apps/common/middleware.py apps/api/route_gates.py pyproject.toml uv.lock tests/test_mail_inbound.py && git commit -m "feat(mail): SNS-verified inbound endpoint and per-recipient routing"`

---

### Task 7: Forwarding (`forward.py`)

**Files:**
- Create: `apps/mail/forward.py`
- Test: `tests/test_mail_forward.py`

**Interfaces:**
- Consumes: `ses.send_raw`, `parse.Parsed`, `wsvc.is_member`.
- Produces: `forward(route: MailRoute, parsed: Parsed, raw: bytes, recipient: str) -> str` returning `forwarded` | `duplicate` | `dropped:not-a-member` | raises on transient SES failure (so SNS retries); `build(route, parsed, raw, recipient) -> bytes`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_forward.py
from __future__ import annotations

from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
from django.contrib.auth.models import User

from apps.mail import forward, parse, ses
from apps.mail.models import MailDomain, MailForwardLog, MailRoute
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
FIX = Path(__file__).parent / "fixtures" / "mail"


@pytest.fixture()
def route(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    u = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=u)
    WorkspaceMembership.objects.create(workspace=ws, user=u, role="owner")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    sent = []
    monkeypatch.setattr(ses, "send_raw", lambda **kw: sent.append(kw) or "ses-fwd-1")
    r = MailRoute.objects.create(domain=d, local_part="*", kind="forward", forward_to=u)
    r.sent = sent
    return r


def test_forward_rewrites_from_and_keeps_reply_to(route):
    raw = (FIX / "with_attachment.eml").read_bytes()
    out = forward.forward(route, parse.parse(raw), raw, "apple@marshellis.com")
    assert out == "forwarded"
    sent = route.sent[0]
    assert sent["from_addr"] == "canopy-forwarder@marshellis.com"
    assert sent["to"] == ["jjackson@dimagi.com"]
    msg = BytesParser(policy=policy.default).parsebytes(sent["raw"])
    assert "via marshellis.com" in msg["From"]
    assert msg["Reply-To"]  # the original sender
    assert msg["X-Original-To"] == "apple@marshellis.com"
    assert [p.get_filename() for p in msg.iter_attachments()] == ["a.pdf"]
    assert not msg.get("DKIM-Signature")  # the original's signature would now fail


def test_redelivery_does_not_forward_twice(route):
    raw = (FIX / "plain.eml").read_bytes()
    p = parse.parse(raw)
    assert forward.forward(route, p, raw, "a@marshellis.com") == "forwarded"
    assert forward.forward(route, p, raw, "a@marshellis.com") == "duplicate"
    assert len(route.sent) == 1


def test_ex_member_stops_receiving(route):
    WorkspaceMembership.objects.filter(user=route.forward_to).delete()
    raw = (FIX / "plain.eml").read_bytes()
    assert forward.forward(route, parse.parse(raw), raw, "a@marshellis.com") == "dropped:not-a-member"
    assert route.sent == []
    assert MailForwardLog.objects.get().status == "dropped"


def test_transient_failure_raises_and_leaves_no_success_row(route, monkeypatch):
    def boom(**kw):
        raise RuntimeError("throttled")
    monkeypatch.setattr(ses, "send_raw", boom)
    raw = (FIX / "plain.eml").read_bytes()
    with pytest.raises(RuntimeError):
        forward.forward(route, parse.parse(raw), raw, "a@marshellis.com")
    assert not MailForwardLog.objects.filter(status="forwarded").exists()
```

- [ ] **Step 2: Run** — Expected: FAIL (import)

- [ ] **Step 3: Implement**

```python
# apps/mail/forward.py
"""Forward a message to a workspace member, as the domain.

SES can only send as a verified identity, so the copy cannot keep the original
From; it says who it is from in the display name and carries Reply-To so a reply
reaches the original sender. The original DKIM-Signature is removed — it no
longer verifies over the rewritten headers.
"""
from __future__ import annotations

from email import policy
from email.parser import BytesParser
from email.utils import formataddr

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.workspaces import services as wsvc

from . import ses
from .models import MailForwardLog

_STRIP = ("DKIM-Signature", "ARC-Seal", "ARC-Message-Signature", "ARC-Authentication-Results",
          "Return-Path", "Sender", "From", "Reply-To", "Message-ID")


def build(route, parsed, raw: bytes, recipient: str) -> bytes:
    msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
    for h in _STRIP:
        del msg[h]
    who = parsed.from_name or parsed.from_addr
    msg["From"] = formataddr((f"{who} via {route.domain.name}",
                              f"{settings.MAIL_FORWARDER_LOCAL_PART}@{route.domain.name}"))
    if parsed.from_addr:
        msg["Reply-To"] = formataddr((parsed.from_name, parsed.from_addr))
    msg["X-Original-From"] = parsed.from_addr
    msg["X-Original-To"] = recipient
    msg["Message-ID"] = f"<fwd-{parsed.message_id.strip('<>')}>"
    return msg.as_bytes()


def _log(route, parsed, recipient, status, error="", destination=None) -> MailForwardLog | None:
    try:
        with transaction.atomic():
            return MailForwardLog.objects.create(
                domain=route.domain, route=route, message_id=parsed.message_id, recipient=recipient,
                original_from=parsed.from_addr, subject=parsed.subject[:998],
                destination=destination, status=status, error=error)
    except IntegrityError:
        return None


def forward(route, parsed, raw: bytes, recipient: str) -> str:
    if MailForwardLog.objects.filter(domain=route.domain, message_id=parsed.message_id,
                                     recipient=recipient).exclude(status=MailForwardLog.FAILED).exists():
        return "duplicate"
    user = route.forward_to
    if user is None or not wsvc.is_member(user, route.domain.workspace):
        _log(route, parsed, recipient, MailForwardLog.DROPPED,
             "destination is no longer a member of this workspace", destination=user)
        return "dropped:not-a-member"
    MailForwardLog.objects.filter(domain=route.domain, message_id=parsed.message_id,
                                  recipient=recipient, status=MailForwardLog.FAILED).delete()
    ses.send_raw(from_addr=f"{settings.MAIL_FORWARDER_LOCAL_PART}@{route.domain.name}",
                 to=[user.email], raw=build(route, parsed, raw, recipient))
    _log(route, parsed, recipient, MailForwardLog.FORWARDED, destination=user)
    return "forwarded"
```

- [ ] **Step 4: Run** `uv run pytest tests/test_mail_forward.py -v` — Expected: PASS
- [ ] **Step 5: Commit** `git add apps/mail/forward.py tests/test_mail_forward.py && git commit -m "feat(mail): forward to workspace members as the domain"`

---

### Task 8: Agent mail in, and the email turn (`agent_mail.deliver` + harness `authserv_id`)

**Files:**
- Create: `apps/mail/agent_mail.py`
- Modify: `apps/harness/services.py` (`enqueue_turn` gains `authserv_id: str | None = None`, threaded to `_record_email_contact`, which passes `authserv_id or settings.INBOUND_EMAIL_AUTHSERV_ID`). The REST `TurnIn` schema is NOT changed.
- Test: `tests/test_mail_agent_turn.py`, plus one test in `tests/test_contacts.py` (or the harness email-contact test file) that a REST-posted turn cannot select `amazonses.com`.

**Interfaces:**
- Consumes: `threads.place`, `ses.keep`, `ses.put_object`, `harness.services.enqueue_turn`.
- Produces: `agent_mail.deliver(route, parsed, raw_key: str) -> str` returning `agent` | `duplicate` | `stored:own-mail` | `stored:automated`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_agent_turn.py
from __future__ import annotations

from pathlib import Path

import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent
from apps.harness.models import Turn
from apps.mail import agent_mail, parse, ses
from apps.mail.models import MailDomain, MailMessage, MailRoute
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
FIX = Path(__file__).parent / "fixtures" / "mail"


@pytest.fixture()
def route(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    monkeypatch.setattr(ses, "keep", lambda k: "kept/" + k.split("/", 1)[-1])
    monkeypatch.setattr(ses, "put_object", lambda *a, **k: None)
    u = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=u)
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws, owner=u)
    return MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)


def test_agent_mail_is_stored_and_becomes_one_email_turn(route):
    p = parse.parse((FIX / "plain.eml").read_bytes())
    assert agent_mail.deliver(route, p, "incoming/abc") == "agent"
    m = MailMessage.objects.get()
    assert m.raw_key == "kept/abc" and m.thread.message_count == 1
    t = Turn.objects.get()
    assert t.origin == Turn.ORIGIN_EMAIL
    assert t.origin_ref["thread_id"] == str(m.thread.uuid)
    assert t.origin_ref["backend"] == "ses"
    assert t.prompt == f"/mada:turn --thread {m.thread.uuid}"


def test_redelivery_is_a_duplicate_not_a_second_turn(route):
    p = parse.parse((FIX / "plain.eml").read_bytes())
    agent_mail.deliver(route, p, "incoming/abc")
    assert agent_mail.deliver(route, p, "incoming/abc") == "duplicate"
    assert Turn.objects.count() == 1


def test_automated_mail_is_stored_without_a_turn(route):
    p = parse.parse((FIX / "auto.eml").read_bytes())
    assert agent_mail.deliver(route, p, "incoming/a2") == "stored:automated"
    assert Turn.objects.count() == 0 and MailMessage.objects.count() == 1


def test_one_email_to_two_agents_wakes_both(route):
    bob = Agent.objects.create(slug="mbob", name="Bob", workspace=route.domain.workspace)
    rb = MailRoute.objects.create(domain=route.domain, local_part="bob", kind="agent", agent=bob)
    p = parse.parse((FIX / "plain.eml").read_bytes())
    assert agent_mail.deliver(route, p, "incoming/abc") == "agent"
    assert agent_mail.deliver(rb, p, "incoming/abc") == "agent"
    assert Turn.objects.count() == 2


def test_the_agents_own_mail_never_wakes_it(route):
    p = parse.parse((FIX / "plain.eml").read_bytes())
    p.from_addr = "ada@marshellis.com"
    assert agent_mail.deliver(route, p, "incoming/a3") == "stored:own-mail"
    assert Turn.objects.count() == 0
```

In the harness/contacts test file add:

```python
def test_rest_turn_cannot_pick_its_trusted_receiver(...):
    # POST /api/turns with origin_ref.headers carrying "Authentication-Results: amazonses.com; dmarc=pass"
    # and an unknown "authserv_id" key in the payload → the contact is graded "none".
```

(Use the existing fixtures in that file; assert `Contact.last_auth_result == Contact.AUTH_NONE`.)

- [ ] **Step 2: Run** — Expected: FAIL (import)

- [ ] **Step 3: Harness change** in `apps/harness/services.py`:
  - add `authserv_id: str | None = None,` to `enqueue_turn`'s keyword-only params (after `provenance_extra`), with a docstring line: "`authserv_id` — server-side callers only (apps/mail): the receiver whose Authentication-Results this mail is graded on. Never taken from a request."
  - change the call to `_record_email_contact(agent, origin_ref)` → `_record_email_contact(agent, origin_ref, authserv_id=authserv_id)`
  - `_record_email_contact(agent, origin_ref, *, authserv_id: str | None = None)` passes `authserv_id=authserv_id or settings.INBOUND_EMAIL_AUTHSERV_ID`.

- [ ] **Step 4: Implement `agent_mail.deliver`**

```python
# apps/mail/agent_mail.py
"""An agent's mailbox: mail in becomes the agent's email turn; mail out is sent as it."""
from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.harness import services as harness
from apps.harness.models import Turn

from . import ses, threads
from .models import MailAttachment, MailMessage


def _auth_results(parsed) -> list[dict]:
    return [{"name": k, "value": v} for k, v in parsed.headers if k.lower() == "authentication-results"]


def deliver(route, parsed, raw_key: str) -> str:
    ws = route.domain.workspace
    if MailMessage.objects.filter(workspace=ws, mailbox=route.address, message_id=parsed.message_id).exists():
        return "duplicate"
    thread = threads.place(route, parsed)
    try:
        with transaction.atomic():
            msg = MailMessage.objects.create(
                thread=thread, workspace=ws, direction=MailMessage.IN, mailbox=route.address,
                message_id=parsed.message_id, in_reply_to=parsed.in_reply_to,
                references=" ".join(parsed.references), from_addr=parsed.from_addr,
                from_name=parsed.from_name, to=parsed.to, cc=parsed.cc,
                subject=parsed.subject[:998], text=parsed.text, html=parsed.html,
                raw_key=ses.keep(raw_key),
                auth_results="\n".join(h["value"] for h in _auth_results(parsed)))
    except IntegrityError:
        return "duplicate"
    for i, att in enumerate(parsed.attachments):
        key = f"attachments/{ws.slug}/{msg.pk}/{i}"
        ses.put_object(key, att.data, att.content_type)
        MailAttachment.objects.create(message=msg, filename=att.filename[:255],
                                      content_type=att.content_type[:255], size=len(att.data), s3_key=key)
    type(thread).objects.filter(pk=thread.pk).update(
        message_count=F("message_count") + 1, last_message_at=timezone.now(), unread=True)
    thread.refresh_from_db()
    if parsed.from_addr == route.address:
        return "stored:own-mail"
    if parsed.automated:
        return "stored:automated"
    agent = route.agent
    harness.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL,
        idempotency_key=f"email-{agent.slug}-{thread.uuid}-{thread.message_count}",
        prompt=f"/{agent.slug}:turn --thread {thread.uuid}",
        origin_ref={"thread_id": str(thread.uuid), "backend": "ses", "from": parsed.from_addr,
                    "from_name": parsed.from_name, "subject": parsed.subject,
                    "message_id": parsed.message_id, "discovered_by": "ses",
                    "message_count": thread.message_count, "headers": _auth_results(parsed)},
        authserv_id=settings.MAIL_SES_AUTHSERV_ID,
    )
    return "agent"
```

- [ ] **Step 5: Run** `uv run pytest tests/test_mail_agent_turn.py tests/test_contacts.py -v` (and the harness test you touched) — Expected: PASS
- [ ] **Step 6: Commit** `git add apps/mail/agent_mail.py apps/harness/services.py tests/ && git commit -m "feat(mail): agent mail becomes an email turn; server-only authserv_id"`

---

### Task 9: Agent read/send REST + `resolve`

**Files:**
- Modify: `apps/mail/agent_mail.py` (add `send`), `apps/mail/schemas.py`, `apps/mail/api.py`, `apps/api/route_gates.py`, `frontend/src/api/generated.ts`
- Test: `tests/test_mail_agent_api.py`

**Interfaces:**
- Produces:
  - `agent_mail.send(route, *, to: list[str], cc: list[str], subject: str, text: str, html: str = "", thread: MailThread | None = None, attachments: list[parse.Attachment] = []) -> MailMessage` raising `agent_mail.SendRefused(status, msg)` for suspended/pending domain (409) or cap reached (429).
  - REST: `GET /api/mail/resolve?address=` (`resolve_mail_address`, authenticated; returns `{backend:"ses", workspace, agent}` only if the caller is a member of that workspace, else 404), `GET /api/mail/threads/{workspace}?agent=&archived=` (`list_mail_threads`, member), `GET /api/mail/threads/{workspace}/{uuid}` (`get_mail_thread`, member; attachments as presigned URLs), `POST /api/mail/threads/{workspace}/{uuid}/reply` (`reply_mail_thread`, agent.work), `POST /api/mail/send/{workspace}` (`send_mail`, agent.work; body has `agent`), `POST /api/mail/threads/{workspace}/{uuid}/state` (`set_mail_thread_state`, agent.work; `{unread?, archived?}`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_agent_api.py
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.mail import ses
from apps.mail.models import MailDomain, MailMessage, MailRoute, MailThread
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w(settings, monkeypatch):
    settings.MAIL_ENABLED = True
    sent = []
    monkeypatch.setattr(ses, "send_raw", lambda **kw: sent.append(kw) or f"ses-{len(sent)}")
    owner = User.objects.create_user("jj", "jjackson@dimagi.com", "pw")
    viewer = User.objects.create_user("v", "v@x.com", "pw")
    ws = Workspace.objects.create(slug="marshellis", display_name="M", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role="viewer")
    d = MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified", daily_send_cap=2)
    a = Agent.objects.create(slug="mada", name="Ada", workspace=ws, owner=owner)
    r = MailRoute.objects.create(domain=d, local_part="ada", kind="agent", agent=a)
    t = MailThread.objects.create(workspace=ws, route=r, subject="Hello", message_count=1)
    MailMessage.objects.create(thread=t, direction="in", message_id="<in-1@x.org>",
                               from_addr="x@x.org", subject="Hello", text="hi")
    return type("W", (), dict(owner=owner, viewer=viewer, d=d, t=t, sent=sent))


def _c(u):
    c = Client(); c.force_login(u); return c


def test_resolve_names_the_ses_backend_for_members_only(w):
    r = _c(w.owner).get("/api/mail/resolve", {"address": "ada@marshellis.com"})
    assert r.json() == {"backend": "ses", "workspace": "marshellis", "agent": "mada"}
    stranger = User.objects.create_user("s", "s@s.com", "pw")
    assert _c(stranger).get("/api/mail/resolve", {"address": "ada@marshellis.com"}).status_code == 404
    assert _c(w.owner).get("/api/mail/resolve", {"address": "eva@dimagi-ai.com"}).status_code == 404


def test_reply_threads_and_sends_as_the_agent(w):
    r = _c(w.owner).post(f"/api/mail/threads/marshellis/{w.t.uuid}/reply",
                         {"text": "Thanks!"}, content_type="application/json")
    assert r.status_code == 200, r.content
    sent = w.sent[0]
    assert sent["from_addr"] == "ada@marshellis.com" and sent["to"] == ["x@x.org"]
    assert b"In-Reply-To: <in-1@x.org>" in sent["raw"]
    out = MailMessage.objects.get(direction="out")
    assert out.thread == w.t and out.ses_message_id == "ses-1" and out.delivery_status == "sent"


def test_viewer_can_read_but_not_send(w):
    assert _c(w.viewer).get(f"/api/mail/threads/marshellis/{w.t.uuid}").status_code == 200
    r = _c(w.viewer).post(f"/api/mail/threads/marshellis/{w.t.uuid}/reply", {"text": "x"},
                          content_type="application/json")
    assert r.status_code == 403


def test_daily_cap_is_enforced(w):
    for _ in range(2):
        assert _c(w.owner).post(f"/api/mail/threads/marshellis/{w.t.uuid}/reply", {"text": "x"},
                                content_type="application/json").status_code == 200
    r = _c(w.owner).post(f"/api/mail/threads/marshellis/{w.t.uuid}/reply", {"text": "x"},
                         content_type="application/json")
    assert r.status_code == 429


def test_suspended_domain_refuses_sends(w):
    MailDomain.objects.filter(pk=w.d.pk).update(status="suspended", suspended_reason="bounces")
    r = _c(w.owner).post(f"/api/mail/threads/marshellis/{w.t.uuid}/reply", {"text": "x"},
                         content_type="application/json")
    assert r.status_code == 409 and "bounces" in r.json()["detail"]
```

- [ ] **Step 2: Run** — Expected: FAIL (404s)

- [ ] **Step 3: Implement `send`** (append to `apps/mail/agent_mail.py`):

```python
import datetime as dt
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .models import MailDomain, MailThread


class SendRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _sent_today(domain) -> int:
    since = timezone.now() - dt.timedelta(days=1)
    return MailMessage.objects.filter(thread__route__domain=domain, direction=MailMessage.OUT,
                                      created_at__gte=since).count()


def send(route, *, to, cc=(), subject="", text="", html="", thread=None, attachments=()) -> MailMessage:
    domain = route.domain
    if domain.status == MailDomain.SUSPENDED:
        raise SendRefused(409, f"sending from {domain.name} is suspended: {domain.suspended_reason}")
    if domain.status != MailDomain.VERIFIED:
        raise SendRefused(409, f"{domain.name} is not verified yet")
    if _sent_today(domain) >= domain.daily_send_cap:
        raise SendRefused(429, f"{domain.name} reached its daily send cap ({domain.daily_send_cap})")
    to = [a.strip().lower() for a in to if a.strip()]
    if not to:
        raise SendRefused(422, "no recipients")
    msg = EmailMessage()
    msg["From"] = formataddr((route.agent.name if route.agent_id else "", route.address))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=domain.name)
    if thread is not None:
        prior = list(thread.messages.order_by("created_at").values_list("message_id", flat=True))
        if prior:
            msg["In-Reply-To"] = prior[-1]
            msg["References"] = " ".join(prior[-20:])
        subject = subject or (thread.subject if thread.subject.lower().startswith("re:")
                              else f"Re: {thread.subject}")
    msg["Subject"] = subject
    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for att in attachments:
        main, _, sub = (att.content_type or "application/octet-stream").partition("/")
        msg.add_attachment(att.data, maintype=main, subtype=sub or "octet-stream", filename=att.filename)
    ses_id = ses.send_raw(from_addr=route.address, to=list(to) + list(cc), raw=msg.as_bytes())
    if thread is None:
        thread = MailThread.objects.create(workspace=domain.workspace, route=route, subject=subject[:998])
    out = MailMessage.objects.create(
        thread=thread, workspace=domain.workspace, direction=MailMessage.OUT,
        message_id=msg["Message-ID"], in_reply_to=msg.get("In-Reply-To", ""),
        references=msg.get("References", ""), from_addr=route.address, to=to, cc=list(cc),
        subject=subject[:998], text=text, html=html, ses_message_id=ses_id,
        delivery_status=MailMessage.SENT)
    MailThread.objects.filter(pk=thread.pk).update(
        message_count=F("message_count") + 1, last_message_at=timezone.now(), unread=False)
    return out
```

A reply's recipients default to the newest inbound message's `from_addr` when the request names none.

- [ ] **Step 4: Routes** (append to `apps/mail/api.py`; add the schemas `MailThreadOut`, `MailThreadListOut`, `MailMessageOut` (with `attachments: list[{filename, content_type, size, url}]`), `MailReplyIn{text, html="", to: list[str] = [], cc: list[str] = []}`, `MailSendIn{agent, to, cc=[], subject, text, html=""}`, `MailThreadStateIn{unread: bool|None, archived: bool|None}`, `MailResolveOut{backend, workspace, agent}` to `schemas.py`):

```python
from apps.agents.models import Agent

from . import agent_mail, ses
from .models import MailThread


def _thread(ws, uuid) -> MailThread:
    t = MailThread.objects.filter(workspace=ws, uuid=uuid).select_related("route__agent", "route__domain").first()
    if t is None:
        raise HttpError(404, "no such thread")
    return t


def _need(user, ws, cap):
    if not perms.can(user, ws, cap):
        raise HttpError(403, "not allowed")


def _msg_out(m) -> dict:
    atts = [{"filename": a.filename, "content_type": a.content_type, "size": a.size,
             "url": ses.presign(a.s3_key, a.filename)} for a in m.attachments.all()]
    return {"id": m.pk, "direction": m.direction, "message_id": m.message_id, "from_addr": m.from_addr,
            "from_name": m.from_name, "to": m.to, "cc": m.cc, "subject": m.subject, "text": m.text,
            "html": m.html, "delivery_status": m.delivery_status, "created_at": m.created_at.isoformat(),
            "attachments": atts}


def _thread_out(t, *, messages: bool) -> dict:
    out = {"thread_id": str(t.uuid), "address": t.route.address,
           "agent": t.route.agent.slug if t.route.agent_id else "", "subject": t.subject,
           "message_count": t.message_count, "unread": t.unread, "archived": t.archived,
           "last_message_at": t.last_message_at.isoformat() if t.last_message_at else ""}
    if messages:
        out["messages"] = [_msg_out(m) for m in t.messages.prefetch_related("attachments")]
    return out


@router.get("/resolve", response=MailResolveOut, summary="Which mail backend serves an address")
def resolve_mail_address(request: HttpRequest, address: str) -> dict:
    domain, route = domains.resolve(address)
    if (route is None or route.kind != MailRoute.AGENT
            or domain.workspace_id not in wsvc.user_workspace_slugs(request.user)):
        raise HttpError(404, "not an SES mailbox")
    return {"backend": "ses", "workspace": domain.workspace_id, "agent": route.agent.slug}


@router.get("/threads/{workspace}", response=MailThreadListOut, summary="List mail threads")
def list_mail_threads(request: HttpRequest, workspace: str, agent: str = "", archived: bool = False) -> dict:
    ws = _ws(request.user, workspace)
    qs = MailThread.objects.filter(workspace=ws, archived=archived).select_related("route__agent", "route__domain")
    if agent:
        qs = qs.filter(route__agent__slug=agent)
    return {"items": [_thread_out(t, messages=False) for t in qs[:200]]}


@router.get("/threads/{workspace}/{uuid}", response=MailThreadOut, summary="Read a mail thread")
def get_mail_thread(request: HttpRequest, workspace: str, uuid: str) -> dict:
    return _thread_out(_thread(_ws(request.user, workspace), uuid), messages=True)


@router.post("/threads/{workspace}/{uuid}/reply", response=MailMessageOut, summary="Reply on a thread as its agent")
def reply_mail_thread(request: HttpRequest, workspace: str, uuid: str, payload: MailReplyIn) -> dict:
    ws = _ws(request.user, workspace)
    _need(request.user, ws, perms.AGENT_WORK)
    t = _thread(ws, uuid)
    to = payload.to or [m.from_addr for m in t.messages.filter(direction="in").order_by("-created_at")[:1]]
    try:
        m = agent_mail.send(t.route, to=to, cc=payload.cc, text=payload.text, html=payload.html, thread=t)
    except agent_mail.SendRefused as exc:
        raise HttpError(exc.status, str(exc)) from None
    return _msg_out(m)


@router.post("/send/{workspace}", response=MailMessageOut, summary="Start a new thread as an agent")
def send_mail(request: HttpRequest, workspace: str, payload: MailSendIn) -> dict:
    ws = _ws(request.user, workspace)
    _need(request.user, ws, perms.AGENT_WORK)
    route = MailRoute.objects.filter(domain__workspace=ws, kind=MailRoute.AGENT, agent__slug=payload.agent,
                                     enabled=True).select_related("domain", "agent").first()
    if route is None:
        raise HttpError(404, f"agent {payload.agent!r} has no mail address here")
    try:
        m = agent_mail.send(route, to=payload.to, cc=payload.cc, subject=payload.subject,
                            text=payload.text, html=payload.html)
    except agent_mail.SendRefused as exc:
        raise HttpError(exc.status, str(exc)) from None
    return _msg_out(m)


@router.post("/threads/{workspace}/{uuid}/state", response=MailThreadOut, summary="Mark a thread read/archived")
def set_mail_thread_state(request: HttpRequest, workspace: str, uuid: str, payload: MailThreadStateIn) -> dict:
    ws = _ws(request.user, workspace)
    _need(request.user, ws, perms.AGENT_WORK)
    t = _thread(ws, uuid)
    if payload.unread is not None:
        t.unread = payload.unread
    if payload.archived is not None:
        t.archived = payload.archived
    t.save(update_fields=["unread", "archived"])
    return _thread_out(t, messages=False)
```

Gates: `"resolve_mail_address": ("authenticated",)`, `"list_mail_threads": ("member",)`, `"get_mail_thread": ("member",)`, `"reply_mail_thread": ("agent.work",)`, `"send_mail": ("agent.work",)`, `"set_mail_thread_state": ("agent.work",)`; `NOT_SETTINGS`: the three writes → `"conversation"`.

Note `MailDomain.workspace_id` is the workspace slug (Workspace PK is `slug` — verify in `apps/workspaces/models.py`; if not, compare `domain.workspace.slug`).

- [ ] **Step 5: Run** `uv run pytest tests/test_mail_agent_api.py tests/test_every_route_declares_its_gate.py -v` — Expected: PASS
- [ ] **Step 6: Regenerate** `cd frontend && npm run gen:api:local && cd ..`
- [ ] **Step 7: Commit** `git add apps/mail apps/api frontend/src/api/generated.ts tests/test_mail_agent_api.py && git commit -m "feat(mail): agent read/send REST and backend resolve"`

---

### Task 10: Reputation guard (`reputation.py` + events endpoint)

**Files:**
- Create: `apps/mail/reputation.py`
- Modify: `apps/mail/api.py` (`ses_events`, auth=None, same verify-first shape as `ses_inbound` but against `MAIL_EVENTS_TOPIC_ARN`), `apps/api/route_gates.py` (`"ses_events": ("host",)`, NOT_SETTINGS `"telemetry"`)
- Test: `tests/test_mail_reputation.py`

**Interfaces:**
- Produces: `reputation.record(event: dict) -> None` (SES event publishing JSON: `eventType`, `mail.messageId`, `mail.source`, `bounce.bounceType`); `reputation.evaluate(domain, *, now=None) -> str | None` (suspension reason or None; applies it).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mail_reputation.py
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.mail import reputation
from apps.mail.models import MailDomain, MailEvent
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def d():
    u = User.objects.create_user("jj", "jj@x.com", "pw")
    ws = Workspace.objects.create(slug="m", display_name="M", created_by=u)
    return MailDomain.objects.create(workspace=ws, name="marshellis.com", status="verified")


def _events(d, kind, n, *, hard=False, ago=dt.timedelta(hours=2)):
    at = timezone.now() - ago
    MailEvent.objects.bulk_create([MailEvent(domain=d, kind=kind, hard=hard, at=at) for _ in range(n)])


def test_under_minimum_volume_never_suspends(d):
    _events(d, "Send", 10); _events(d, "Bounce", 5)
    assert reputation.evaluate(d) is None


def test_bounce_rate_over_4_percent_suspends(d):
    _events(d, "Send", 100); _events(d, "Bounce", 5)
    assert "bounce" in reputation.evaluate(d)
    d.refresh_from_db(); assert d.status == "suspended"


def test_complaint_rate_over_0_08_percent_suspends(d):
    _events(d, "Send", 2000); _events(d, "Complaint", 2)
    assert "complaint" in reputation.evaluate(d)


def test_hard_bounce_burst_suspends_regardless_of_volume(d):
    _events(d, "Bounce", 11, hard=True, ago=dt.timedelta(minutes=10))
    assert "burst" in reputation.evaluate(d)


def test_record_maps_an_ses_event_to_the_sending_domain(d):
    reputation.record({"eventType": "Bounce", "mail": {"messageId": "m1", "source": "ada@marshellis.com",
                                                        "timestamp": timezone.now().isoformat()},
                       "bounce": {"bounceType": "Permanent"}})
    e = MailEvent.objects.get(); assert e.kind == "Bounce" and e.hard
```

- [ ] **Step 2: Run** — Expected: FAIL (import)

- [ ] **Step 3: Implement**

```python
# apps/mail/reputation.py
"""Keep one tenant from spending the labs account's sending reputation.

Thresholds sit under SES's own review lines (5 % bounce, 0.1 % complaint) so a
domain is stopped here before AWS pauses sending for connect-labs too.
"""
from __future__ import annotations

import datetime as dt

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import MailDomain, MailEvent, MailMessage

WINDOW = dt.timedelta(days=7)
MIN_SENDS = 50
MAX_BOUNCE = 0.04
MAX_COMPLAINT = 0.0008
BURST_WINDOW = dt.timedelta(hours=1)
BURST_HARD_BOUNCES = 10
_STATUS = {"Delivery": MailMessage.DELIVERED, "Bounce": MailMessage.BOUNCED,
           "Complaint": MailMessage.COMPLAINED, "Reject": MailMessage.REJECTED}


def _domain_of(source: str) -> MailDomain | None:
    host = source.rpartition("@")[2].lower().strip("> ")
    return MailDomain.objects.filter(name=host).first()


def record(event: dict) -> None:
    kind = event.get("eventType") or event.get("notificationType") or ""
    mail = event.get("mail") or {}
    domain = _domain_of(mail.get("source", ""))
    if domain is None or kind not in ("Send", "Delivery", "Bounce", "Complaint", "Reject"):
        return
    at = parse_datetime(mail.get("timestamp") or "") or timezone.now()
    hard = kind == "Bounce" and (event.get("bounce") or {}).get("bounceType") == "Permanent"
    MailEvent.objects.create(domain=domain, kind=kind, hard=hard,
                             ses_message_id=mail.get("messageId", ""), at=at)
    if kind in _STATUS:
        MailMessage.objects.filter(ses_message_id=mail.get("messageId", "")).update(delivery_status=_STATUS[kind])
    evaluate(domain)


def evaluate(domain: MailDomain, *, now=None) -> str | None:
    now = now or timezone.now()
    ev = MailEvent.objects.filter(domain=domain, at__gte=now - WINDOW)
    sends = ev.filter(kind="Send").count()
    reason = None
    burst = ev.filter(kind="Bounce", hard=True, at__gte=now - BURST_WINDOW).count()
    if burst > BURST_HARD_BOUNCES:
        reason = f"hard-bounce burst: {burst} in the last hour"
    elif sends >= MIN_SENDS:
        bounces = ev.filter(kind="Bounce").count() / sends
        complaints = ev.filter(kind="Complaint").count() / sends
        if bounces > MAX_BOUNCE:
            reason = f"bounce rate {bounces:.1%} over 7 days (limit {MAX_BOUNCE:.0%})"
        elif complaints > MAX_COMPLAINT:
            reason = f"complaint rate {complaints:.2%} over 7 days (limit {MAX_COMPLAINT:.2%})"
    if reason and domain.status != MailDomain.SUSPENDED:
        domain.status, domain.suspended_reason = MailDomain.SUSPENDED, reason
        domain.save(update_fields=["status", "suspended_reason"])
    return reason
```

Route:

```python
from . import reputation


@router.post("/ses/events/", auth=None, response={200: dict, 404: dict},
             summary="SES sending events (SNS HTTPS delivery)")
def ses_events(request: HttpRequest):
    try:
        msg = json.loads(request.body or b"{}")
        sns.verify(msg, topic_arn=settings.MAIL_EVENTS_TOPIC_ARN)
    except (ValueError, sns.Invalid):
        return 404, {"detail": "not found"}
    if msg.get("Type") == "SubscriptionConfirmation":
        sns.confirm(msg)
        return 200, {"confirmed": True}
    if msg.get("Type") == "Notification":
        reputation.record(json.loads(msg.get("Message") or "{}"))
    return 200, {}
```

The 0.08% test: 2 complaints / 2000 sends = 0.1% > 0.08% ✓.

- [ ] **Step 4: Run** `uv run pytest tests/test_mail_reputation.py tests/test_every_route_declares_its_gate.py -v` — Expected: PASS
- [ ] **Step 5: Regenerate types + commit** `cd frontend && npm run gen:api:local && cd .. && git add -A apps/mail apps/api frontend/src/api/generated.ts tests/test_mail_reputation.py && git commit -m "feat(mail): reputation guard — SES events, auto-suspension"`

---

### Task 11: Settings → Email page

**Files:**
- Create: `frontend/src/api/mail.ts`, `frontend/src/pages/WorkspaceEmailPage.tsx`, `frontend/src/pages/WorkspaceEmailPage.test.tsx`
- Modify: `frontend/src/router.tsx` (child `{ path: 'email', element: <WorkspaceEmailPage /> }` after `inbound`), `frontend/src/pages/WorkspaceSettingsPage.tsx` (section `{ segment: 'email', label: 'Email' }` after Inbound), `frontend/src/guide/surfaces.ts` (descriptor), `docs/architecture/pages.md` (+ CLAUDE.md `settings/<section>` list gains `email`)

**Interfaces:**
- Consumes: REST from Tasks 4 and 9 via `apiV2` + generated types (`MailDomainOut`, `MailRouteOut`, `DnsRecordOut`).
- Produces: `listMailDomains(ws)`, `addMailDomain(ws, name)`, `refreshMailDomain(ws, id)`, `updateMailDomain(ws, id, body)`, `removeMailDomain(ws, id)`, `saveMailRoute(ws, id, body)`, `deleteMailRoute(ws, id, routeId)`, `listMailThreads(ws, agent?)`, `getMailThread(ws, uuid)`; `class MailApiError extends Error { status }` — same shape as `frontend/src/api/inbound.ts`.

- [ ] **Step 1: Write the failing test** (`vitest` + Testing Library, mocking `@/api/mail` like `SlackSettingsPage.test.tsx` mocks its API module — read that file first and copy its render/mocking harness exactly)

```tsx
// frontend/src/pages/WorkspaceEmailPage.test.tsx
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import WorkspaceEmailPage from './WorkspaceEmailPage'

vi.mock('@/api/mail', () => ({
  MailApiError: class extends Error { status = 0 },
  listMailDomains: vi.fn().mockResolvedValue({
    enabled: true,
    items: [{
      id: 1, name: 'marshellis.com', status: 'pending', receiving: false, mx_provider: 'Zoho',
      suspended_reason: '', daily_send_cap: 200, can_manage: true,
      records: [
        { purpose: 'spf', type: 'TXT', name: 'marshellis.com',
          value: 'v=spf1 include:zohomail.com include:amazonses.com -all', priority: null, ok: false, found: [] },
        { purpose: 'mx', type: 'MX', name: 'marshellis.com',
          value: 'inbound-smtp.us-east-1.amazonaws.com', priority: 10, ok: false, found: ['10 mx.zoho.com.'] },
      ],
      routes: [{ id: 9, local_part: '*', address: '*@marshellis.com', kind: 'forward', agent_slug: '',
                 forward_to_user_id: 1, forward_to_email: 'jjackson@dimagi.com', enabled: true }],
    }],
  }),
}))
vi.mock('@/api/agents', () => ({ listAgents: vi.fn().mockResolvedValue([]) }))
vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<object>()),
  listMembers: vi.fn().mockResolvedValue([{ user_id: 1, email: 'jjackson@dimagi.com', role: 'owner' }]),
}))

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/w/marshellis/settings/email']}>
      <Routes><Route path="/w/:workspace/settings/email" element={<WorkspaceEmailPage />} /></Routes>
    </MemoryRouter>,
  )
}

describe('WorkspaceEmailPage', () => {
  it('warns before an MX switch takes mail from the current provider', async () => {
    renderPage()
    expect(await screen.findByText(/Zoho currently receives mail for this domain/)).toBeInTheDocument()
  })
  it('shows the merged SPF value, never a second SPF record', async () => {
    renderPage()
    expect(await screen.findByText('v=spf1 include:zohomail.com include:amazonses.com -all')).toBeInTheDocument()
  })
  it('lists the catch-all forward to a member', async () => {
    renderPage()
    expect(await screen.findByText('*@marshellis.com')).toBeInTheDocument()
    expect(screen.getByText('jjackson@dimagi.com')).toBeInTheDocument()
  })
})
```

(Use whatever member-list function `@/api/workspaces` actually exports — grep `export async function list` in that file — and adjust the mock name.)

- [ ] **Step 2: Run** `cd frontend && npx vitest run src/pages/WorkspaceEmailPage.test.tsx` — Expected: FAIL (module not found)

- [ ] **Step 3: Implement `frontend/src/api/mail.ts`** — copy `inbound.ts`'s structure:

```ts
// Mail domains API — a workspace's own domain run through SES (apps/mail).
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type MailDomainOut = components['schemas']['MailDomainOut']
export type MailRouteOut = components['schemas']['MailRouteOut']
export type DnsRecordOut = components['schemas']['DnsRecordOut']
export type MailDomainListOut = components['schemas']['MailDomainListOut']
export type MailRouteIn = components['schemas']['MailRouteIn']
export type MailThreadOut = components['schemas']['MailThreadOut']
export type MailThreadListOut = components['schemas']['MailThreadListOut']

export class MailApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function fail(res: { response: Response; error?: unknown }, fallback: string): never {
  throw new MailApiError(res.response.status, problemMessage(res.error, fallback))
}

export async function listMailDomains(workspace: string): Promise<MailDomainListOut> {
  const res = await apiV2.GET('/api/mail/domains/{workspace}', { params: { path: { workspace } } })
  if (!res.response.ok) fail(res, 'Failed to load mail domains')
  return res.data as MailDomainListOut
}

export async function addMailDomain(workspace: string, name: string): Promise<MailDomainOut> {
  const res = await apiV2.POST('/api/mail/domains/{workspace}', { params: { path: { workspace } }, body: { name } })
  if (!res.response.ok) fail(res, 'Failed to add the domain')
  return res.data as MailDomainOut
}

export async function refreshMailDomain(workspace: string, domainId: number): Promise<MailDomainOut> {
  const res = await apiV2.POST('/api/mail/domains/{workspace}/{domain_id}/refresh', {
    params: { path: { workspace, domain_id: domainId } } })
  if (!res.response.ok) fail(res, 'Failed to re-check the domain')
  return res.data as MailDomainOut
}

export async function updateMailDomain(workspace: string, domainId: number,
  body: { daily_send_cap?: number; unsuspend?: boolean }): Promise<MailDomainOut> {
  const res = await apiV2.PATCH('/api/mail/domains/{workspace}/{domain_id}', {
    params: { path: { workspace, domain_id: domainId } }, body })
  if (!res.response.ok) fail(res, 'Failed to update the domain')
  return res.data as MailDomainOut
}

export async function removeMailDomain(workspace: string, domainId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/mail/domains/{workspace}/{domain_id}', {
    params: { path: { workspace, domain_id: domainId } } })
  if (!res.response.ok) fail(res, 'Failed to remove the domain')
}

export async function saveMailRoute(workspace: string, domainId: number, body: MailRouteIn): Promise<MailRouteOut> {
  const res = await apiV2.PUT('/api/mail/domains/{workspace}/{domain_id}/routes', {
    params: { path: { workspace, domain_id: domainId } }, body })
  if (!res.response.ok) fail(res, 'Failed to save the route')
  return res.data as MailRouteOut
}

export async function deleteMailRoute(workspace: string, domainId: number, routeId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/mail/domains/{workspace}/{domain_id}/routes/{route_id}', {
    params: { path: { workspace, domain_id: domainId, route_id: routeId } } })
  if (!res.response.ok) fail(res, 'Failed to remove the route')
}

export async function listMailThreads(workspace: string, agent = ''): Promise<MailThreadListOut> {
  const res = await apiV2.GET('/api/mail/threads/{workspace}', {
    params: { path: { workspace }, query: { agent } } })
  if (!res.response.ok) fail(res, 'Failed to load mail')
  return res.data as MailThreadListOut
}

export async function getMailThread(workspace: string, uuid: string): Promise<MailThreadOut> {
  const res = await apiV2.GET('/api/mail/threads/{workspace}/{uuid}', { params: { path: { workspace, uuid } } })
  if (!res.response.ok) fail(res, 'Failed to load the thread')
  return res.data as MailThreadOut
}
```

- [ ] **Step 4: Implement `WorkspaceEmailPage.tsx`.** Structure (dense, tables; semantic tokens only; shadcn `Button`, `Input`, `Table` from the components the retention page imports — copy its import lines):
  - Header: "Email" + one-line explainer. If `enabled === false`: a `text-muted-foreground` note "Email domains are off on this deployment" and nothing else.
  - No domain + `can_manage`: an input + "Add domain" button → `addMailDomain`.
  - Per domain: a status line — badge `Pending DNS` / `Verified` / `Suspended` (`warning` / `success` / `destructive` tokens), "Receiving: live / not yet", a "Re-check" button (`refreshMailDomain`).
  - If `!receiving && mx_provider`: a warning row rendered exactly as `` `${mx_provider} currently receives mail for this domain — switching MX moves all of it here.` ``
  - If suspended: the reason + "Lift suspension" (`updateMailDomain(..., {unsuspend: true})`, admins only).
  - **DNS records table**: columns Purpose · Type · Name · Value (monospace, with a copy button using `navigator.clipboard.writeText`) · Priority · Status (✓ / ✗ / found values in muted text).
  - **Routes table**: Address · Goes to (agent slug, member email, or "Dropped") · Enabled · actions (edit, remove). An "Add address" row: local-part input (placeholder `jjackson` or `*` for everything else), kind select (Agent / Forward to member / Drop), then an agent select (from `listAgents`) or a member select (from the workspace member list) — **no free-text email field**.
  - Daily send cap: number input (admins), saved via `updateMailDomain`.
  - Errors show `MailApiError.message` inline.

- [ ] **Step 5: Router, section nav, guide descriptor**

```ts
// frontend/src/guide/surfaces.ts — after the inbound entry
  {
    path: '/w/:workspace/settings/email',
    title: 'Email',
    audience: 'Workspace admin bringing their own domain to canopy',
    what: "Run a domain's email through canopy instead of a mail provider. Add the domain, publish the DNS records shown (each is checked live), and once it verifies, choose where each address goes: an agent's mailbox (mail becomes that agent's email turn and it replies as that address), a member of this workspace (forwarded to their canopy login email), or dropped. `*` catches every address not listed. The page warns before the MX switch takes mail from your current provider, caps how much a domain sends per day, and suspends sending automatically if bounces or complaints climb.",
    actions: ['Add a domain and see its DNS records', 'Route an address to an agent or a member', 'Re-check DNS', 'Change the daily send cap or lift a suspension'],
  },
```

- [ ] **Step 6: Run** `cd frontend && npx vitest run src/pages/WorkspaceEmailPage.test.tsx src/routeTenancy.test.ts && npm run build` and the guide coverage test (`npx vitest run src/guide`) — Expected: PASS
- [ ] **Step 7: Commit** `git add frontend docs/architecture/pages.md CLAUDE.md && git commit -m "feat(mail): Settings → Email page"`

---

### Task 12: Agent Mail section (read threads on canopy-web)

**Files:**
- Create: `frontend/src/components/agent/AgentMailSection.tsx` (+ `.test.tsx`)
- Modify: `frontend/src/router.tsx` (agent child `{ path: 'mail', element: <LazySection><AgentMailSection /></LazySection> }` beside `settings` at ~line 350), the agent workspace tab list (find where `Settings` tab is declared in `AgentWorkspacePage.tsx`; add `Mail` — shown only when `listMailThreads` returns or the agent has a route; simplest: always show, empty state says "This agent has no email address — add one in Settings → Email"), `frontend/src/guide/surfaces.ts` (descriptor for `/w/:workspace/agents/:slug/mail`), `docs/architecture/pages.md`

**Interfaces:**
- Consumes: `listMailThreads(ws, agent)`, `getMailThread(ws, uuid)`.

- [ ] **Step 1: Failing test** — renders a thread list (subject, counterpart, count, time) from a mocked `listMailThreads`; clicking a row loads `getMailThread` and shows messages in order with direction labels; an HTML body renders inside `<iframe sandbox="" srcdoc=…>` (no `allow-same-origin`); attachments are links.

```tsx
it('renders html mail in a sandbox without same-origin', async () => {
  // mock getMailThread → one message with html '<p>Hi</p>'
  // click the row; expect(container.querySelector('iframe')?.getAttribute('sandbox')).toBe('')
})
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** a two-pane table + detail (list left, thread right on desktop; stacked on phone). **Step 4: Run** vitest + `npm run build` — PASS.
- [ ] **Step 5: Commit** `git add frontend docs/architecture/pages.md && git commit -m "feat(mail): agent Mail section"`

---

### Task 13: AWS stack, docs, runbook

**Files:**
- Create: `deploy/aws/canopy-mail.cfn.yaml`, `docs/architecture/mail.md`, `docs/runbooks/mail-domain-onboarding.md`
- Modify: `deploy/aws/canopy-web.cfn.yaml` (task env: `MAIL_ENABLED`, `MAIL_INBOUND_BUCKET`, `MAIL_INBOUND_TOPIC_ARN`, `MAIL_EVENTS_TOPIC_ARN` as parameters defaulting to empty/false), CLAUDE.md "Where to read before touching…" (+ one line pointing at `mail.md`), `docs/architecture/api-surface.md` (the `/api/mail` routes)
- Test: `tests/test_mail_stack.py` (parse the YAML with a CFN-tag-tolerant loader and assert the invariants)

- [ ] **Step 1: Failing test**

```python
# tests/test_mail_stack.py
"""The AWS side's invariants, checked on the template so a hand-edit cannot drop one."""
from __future__ import annotations

from pathlib import Path

import yaml

STACK = Path(__file__).resolve().parent.parent / "deploy" / "aws" / "canopy-mail.cfn.yaml"


class _L(yaml.SafeLoader):
    pass


_L.add_multi_constructor("!", lambda loader, suffix, node: None)


def _res():
    return yaml.load(STACK.read_text(), Loader=_L)["Resources"]


def test_incoming_expires_after_7_days_and_kept_does_not():
    rules = _res()["InboundBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    incoming = [r for r in rules if r["Prefix"] == "incoming/"]
    assert incoming and incoming[0]["ExpirationInDays"] == 7
    assert not [r for r in rules if r["Prefix"] == "kept/"]


def test_receipt_rule_scans_requires_tls_and_has_no_recipient_filter():
    rule = _res()["ReceiptRule"]["Properties"]["Rule"]
    assert rule["ScanEnabled"] is True and rule["TlsPolicy"] == "Require"
    assert "Recipients" not in rule


def test_tenant_sends_cannot_use_dimagi_addresses():
    doc = _res()["MailPolicy"]["Properties"]["PolicyDocument"]
    send = next(s for s in doc["Statement"] if "ses:SendRawEmail" in s["Action"])
    assert "StringNotLike" in send["Condition"]
    assert "*@dimagi.com" in send["Condition"]["StringNotLike"]["ses:FromAddress"]
```

- [ ] **Step 2: Run** — FAIL (file missing)

- [ ] **Step 3: Write the stack**

```yaml
AWSTemplateFormatVersion: '2010-09-09'
# Workspace email domains (apps/mail): receive into S3, notify canopy-web over SNS,
# send as tenant domains through their own configuration set.
#
# Deployed by hand with a labs admin login (the deploy role holds no ses:*):
#   aws cloudformation deploy --profile labs --stack-name canopy-web-mail \
#     --template-file deploy/aws/canopy-mail.cfn.yaml --capabilities CAPABILITY_NAMED_IAM
# Then activate the rule set (CloudFormation cannot):
#   aws ses set-active-receipt-rule-set --profile labs --region us-east-1 --rule-set-name canopy-inbound
# Removing the feature: delete this stack (empty the bucket first).
Parameters:
  TaskRoleName:
    Type: String
    Default: labs-jj-ecs-task-role
  CanopyBaseUrl:
    Type: String
    Default: https://labs.connect.dimagi.com/canopy
Resources:
  InboundBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Sub canopy-inbound-mail-${AWS::AccountId}
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - ServerSideEncryptionByDefault: {SSEAlgorithm: AES256}
      LifecycleConfiguration:
        Rules:
          - Id: incoming-7d
            Status: Enabled
            Prefix: incoming/
            ExpirationInDays: 7
  InboundBucketPolicy:
    Type: AWS::S3::BucketPolicy
    Properties:
      Bucket: !Ref InboundBucket
      PolicyDocument:
        Statement:
          - Effect: Allow
            Principal: {Service: ses.amazonaws.com}
            Action: s3:PutObject
            Resource: !Sub ${InboundBucket.Arn}/incoming/*
            Condition:
              StringEquals:
                AWS:SourceAccount: !Ref AWS::AccountId
              ArnLike:
                AWS:SourceArn: !Sub arn:aws:ses:${AWS::Region}:${AWS::AccountId}:receipt-rule-set/canopy-inbound:receipt-rule/canopy-all
  InboundTopic:
    Type: AWS::SNS::Topic
    Properties:
      TopicName: canopy-inbound-mail
  InboundTopicPolicy:
    Type: AWS::SNS::TopicPolicy
    Properties:
      Topics: [!Ref InboundTopic]
      PolicyDocument:
        Statement:
          - Effect: Allow
            Principal: {Service: ses.amazonaws.com}
            Action: sns:Publish
            Resource: !Ref InboundTopic
            Condition:
              StringEquals: {AWS:SourceAccount: !Ref AWS::AccountId}
  InboundSubscription:
    Type: AWS::SNS::Subscription
    Properties:
      TopicArn: !Ref InboundTopic
      Protocol: https
      Endpoint: !Sub ${CanopyBaseUrl}/api/mail/ses/inbound/
      DeliveryPolicy:
        healthyRetryPolicy:
          numRetries: 100
          numMaxDelayRetries: 30
          minDelayTarget: 20
          maxDelayTarget: 600
          backoffFunction: exponential
  RuleSet:
    Type: AWS::SES::ReceiptRuleSet
    Properties:
      RuleSetName: canopy-inbound
  ReceiptRule:
    Type: AWS::SES::ReceiptRule
    DependsOn: [InboundBucketPolicy, InboundTopicPolicy]
    Properties:
      RuleSetName: !Ref RuleSet
      Rule:
        Name: canopy-all
        Enabled: true
        ScanEnabled: true
        TlsPolicy: Require
        Actions:
          - S3Action:
              BucketName: !Ref InboundBucket
              ObjectKeyPrefix: incoming/
              TopicArn: !Ref InboundTopic
  EventsTopic:
    Type: AWS::SNS::Topic
    Properties:
      TopicName: canopy-mail-events
  EventsSubscription:
    Type: AWS::SNS::Subscription
    Properties:
      TopicArn: !Ref EventsTopic
      Protocol: https
      Endpoint: !Sub ${CanopyBaseUrl}/api/mail/ses/events/
  TenantConfigSet:
    Type: AWS::SES::ConfigurationSet
    Properties:
      Name: canopy-tenant-mail
  TenantConfigSetEvents:
    Type: AWS::SES::ConfigurationSetEventDestination
    Properties:
      ConfigurationSetName: !Ref TenantConfigSet
      EventDestination:
        Name: canopy-mail-events
        Enabled: true
        MatchingEventTypes: [send, delivery, bounce, complaint, reject]
        SnsDestination: {TopicARN: !Ref EventsTopic}
  MailPolicy:
    Type: AWS::IAM::Policy
    Properties:
      PolicyName: canopy-mail
      Roles: [!Ref TaskRoleName]
      PolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Action: [ses:CreateEmailIdentity, ses:GetEmailIdentity, ses:DeleteEmailIdentity,
                     ses:PutEmailIdentityMailFromAttributes, ses:PutEmailIdentityDkimSigningAttributes,
                     ses:TagResource]
            Resource: !Sub arn:aws:ses:${AWS::Region}:${AWS::AccountId}:identity/*
          - Effect: Allow
            Action: [ses:SendRawEmail, ses:SendEmail]
            Resource: '*'
            Condition:
              StringNotLike:
                ses:FromAddress: ['*@dimagi.com', '*@*.dimagi.com', '*@dimagi-ai.com']
              StringEquals:
                ses:ConfigurationSet: canopy-tenant-mail
          - Effect: Allow
            Action: [s3:GetObject, s3:PutObject]
            Resource: !Sub ${InboundBucket.Arn}/*
Outputs:
  InboundBucket: {Value: !Ref InboundBucket}
  InboundTopicArn: {Value: !Ref InboundTopic}
  EventsTopicArn: {Value: !Ref EventsTopic}
```

Note for the implementer: the existing `canopy-ses-send` policy already grants unconditioned `ses:SendRawEmail` for `canopy.dimagi.com`; IAM allows are additive, so this condition limits only *this* policy's grant. That is acceptable (canopy-web's own system mail keeps working); the app-level guard is that `agent_mail.send` and `forward` only ever use a `MailDomain` address. Verify the `ses:ConfigurationSet` condition key name against current SES IAM docs before deploying; drop that key if SES rejects it.

`s3:CopyObject` is not an IAM action — `CopyObject` needs `s3:GetObject` on the source and `s3:PutObject` on the target, both granted above.

- [ ] **Step 4: Docs** — `docs/architecture/mail.md` (current-state topic doc: model, flows, gates, the rip-out list from the spec, settings); `docs/runbooks/mail-domain-onboarding.md` (deploy stack → activate rule set → set task env + deploy → add domain in UI → publish DNS → `mail.` subdomain test → routes → MX cutover → retire old provider; and the "remove the feature" steps).
- [ ] **Step 5: Run** `uv run pytest tests/test_mail_stack.py tests/test_claude_md_budget.py -v` — PASS
- [ ] **Step 6: Commit** `git add deploy docs CLAUDE.md tests/test_mail_stack.py && git commit -m "feat(mail): AWS stack, topic doc and onboarding runbook"`

---

### Task 14: Full suite, PR, deploy, AWS stack (operator task)

- [ ] **Step 1:** `uv run pytest` (whole suite) and `cd frontend && npm run build` — all green. Fix anything the global contract tests (`test_every_route_declares_its_gate`, `test_nothing_outside_a_workspace`, `test_architecture_boundary`, guide coverage, `generated.ts` freshness) report.
- [ ] **Step 2:** Push `feat/email-domains`, open the PR (body ends with the 🤖 attribution line), `gh pr merge <n> --auto`, confirm `autoMergeRequest` is set, wait for MERGED.
- [ ] **Step 3:** Deploy the stack (labs admin):
  `aws cloudformation deploy --profile labs --region us-east-1 --stack-name canopy-web-mail --template-file deploy/aws/canopy-mail.cfn.yaml --capabilities CAPABILITY_NAMED_IAM`
  then `aws ses set-active-receipt-rule-set --profile labs --region us-east-1 --rule-set-name canopy-inbound`.
- [ ] **Step 4:** Set the task env (`MAIL_ENABLED=true` + the three outputs) via the canopy-web stack parameters, then `gh workflow run "Deploy to Labs (AWS)" --ref main`; confirm with `gh run list --workflow="Deploy to Labs (AWS)" --limit 3 --json headSha,conclusion` and that `/api/openapi.json` lists `/api/mail/domains/{workspace}`.
- [ ] **Step 5:** Confirm both SNS subscriptions show `Confirmed` (`aws sns list-subscriptions-by-topic`); a `PendingConfirmation` means the endpoint did not verify — check the deploy logs.

---

### Task 15: marshellis.com live test and cutover (operator task, needs the Cloudflare token)

- [ ] **Step 1:** In the UI (`/w/marshellis/settings/email`) add `marshellis.com`. Read back the records.
- [ ] **Step 2:** Show Jonathan the exact Cloudflare changes, then publish via the Cloudflare API with the marshellis-scoped token read from 1Password (`op read`, never echoed): 3 DKIM CNAMEs (DNS-only, not proxied), `bounce` MX + SPF, root SPF **replaced** by the merged value, DMARC, and **`mail.marshellis.com` MX → `inbound-smtp.us-east-1.amazonaws.com`** (root MX untouched — Zoho keeps working).
- [ ] **Step 3:** Re-check until `Verified`. Add routes: `jjackson` → forward jjackson; `support` → forward jjackson; `*` → forward jjackson.
- [ ] **Step 4:** Send a test from an outside account to `test@mail.marshellis.com`; confirm it arrives in jjackson@dimagi.com as "… via marshellis.com" with a working Reply-To, and a `MailForwardLog` row `forwarded`. Check the received `Authentication-Results` authserv-id really is `amazonses.com` (else set `MAIL_SES_AUTHSERV_ID`).
- [ ] **Step 5:** Create a test agent route (`ada@`), mail `ada@mail.marshellis.com`, confirm the turn is enqueued with `origin_ref.backend == "ses"`, reply via `POST /api/mail/threads/marshellis/<uuid>/reply`, and confirm the reply lands threaded in the outside mailbox.
- [ ] **Step 6:** With Jonathan's go: switch the **root** MX to SES (remove Zoho MX), drop `include:zohomail.com` from SPF, re-test `jjackson@marshellis.com` and `support@marshellis.com`. Tell Jonathan Zoho can be cancelled after he exports anything he wants to keep.

---

### Task 16 (canopy repo, separate PR): `canopy email` backend switch

**Repo:** `canopy` (`src/orchestrator/agent_email.py`). Read the canopy repo's CLAUDE.md first (version bump per PR; merge queue).

**Files:**
- Modify: `src/orchestrator/agent_email.py` (add `_backend(mailbox) -> Literal["gmail","ses"]` via `canopy_web.call("GET", "/api/mail/resolve", params={"address": mailbox})` — 404 → `gmail`, cached per process; route `send`, `mark-read`, `archive`, and a new `thread` verb to REST when `ses`)
- Test: `tests/test_agent_email_backend.py`

**Interfaces:**
- Consumes: REST from Task 9: `GET /api/mail/resolve`, `GET /api/mail/threads/{ws}/{uuid}`, `POST /api/mail/threads/{ws}/{uuid}/reply`, `POST /api/mail/send/{ws}`, `POST /api/mail/threads/{ws}/{uuid}/state`.

- [ ] **Step 1: Failing tests** — with `canopy_web.call` monkeypatched: (a) a 404 from resolve keeps the existing `gog gmail send` argv byte-for-byte (snapshot the current argv in the test first); (b) a `ses` resolve sends `POST /api/mail/threads/marshellis/<uuid>/reply` with the body text and never spawns `gog`; (c) `canopy email thread <uuid> --account ada@marshellis.com` prints the thread's messages; (d) resolve failure (network) falls back to `gmail` **loudly** (stderr line), matching the gog-client fallback convention.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS. **Step 5:** version bump, PR, merge, reinstall the engine on the laptop and runners per the canopy dev-workflow.

---

## Self-review notes

- Spec coverage: domain add/DNS/verify (T3–T4), MX warning (T3/T11), routes + member-only forward + reserved local part (T4/T7), SNS verify-first (T6), spam/virus drop (T6), threading (T5), agent turn + auth grading (T8), REST read/send/resolve (T9), cap + suspension (T9/T10), retention 7 d / kept (T13 + T8), UI (T11/T12), rip-out (Global Constraints + T13 runbook), CLI (T16), live test on `mail.marshellis.com` + cutover (T15).
- Review Focus tests: (1) T3 `test_spf_is_merged_never_duplicated`; (2) T6/T7/T8 duplicate tests; (3) T5 encoded/no-id/html-only; (4) T6 `test_each_recipient_routes_once` + T8 own-mail; (5) T7 `test_ex_member_stops_receiving`.
