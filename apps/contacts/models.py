"""A person canopy knows about, who is not (yet) a member of anything.

WHY THIS EXISTS. Someone from a partner organisation emails `ace@dimagi-ai.com`
with a question the agent can answer. For the agent to remember them next time —
and for routing to ever depend on who they are — canopy needs somewhere to put
that person. Today it has exactly two options and both are wrong: leave them
anonymous inside a `Turn`'s `origin_ref`, or make them a `WorkspaceMembership`,
which hands an external partner the entire tenant's agents, projects and
shareouts.

A `Contact` is the missing third thing: canopy knows who you are, and that is
all. It is deliberately NOT a grant.

**A CONTACT IS NEVER A MEMBER, AND THAT IS THE WHOLE SECURITY PROPERTY.**
Creating one confers no access to anything — not the workspace, not the agent,
not even the thread the person themself wrote. It exists so the agent can
remember and so routing has something to read. This matters more than it looks:
this codebase spent a long time removing every path where writing a row granted
membership as a side effect (five create endpoints did, via `ensure_member`),
and "an inbound email creates a user" is that same pattern arriving through the
mail slot. `tests/test_contacts.py` asserts it directly.

**IDENTITY FROM EMAIL IS AN ASSERTION, NOT A FACT.** The `From:` header is
trivially forged. What can be checked is whether the sending DOMAIN authorised
the message — see `email_auth.py` — and that verdict is stored per contact as a
grade rather than a boolean, because partner organisations run mail servers of
wildly varying quality and "unverified" has to remain a workable state rather
than a rejection. Even a perfect `dmarc=pass` proves the domain sent it, not
that the human is who they claim; person-level identity needs an out-of-band
step (`promote_to_user`), which is why that is a separate, deliberate action.

WORKSPACE-OWNED, and a person may be a contact in more than one. The profile is
the tenant's knowledge of that person, governed by the tenant's ACL — not the
agent's private memory. A second agent in the same workspace should benefit from
what the first learned, and an owner should be able to see and correct what
canopy holds about someone. Cross-TENANT knowledge is deliberately not a thing:
two workspaces dealing with the same human hold two profiles, because merging
them would leak one tenant's dealings into another.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Person(models.Model):
    """One PERSON, across every tenant that deals with them.

    A `Contact` is deliberately per workspace — the same human dealt with by two
    tenants is two records, because merging them would leak one tenant's
    dealings into the other. That rule is about what a tenant may SEE, and it
    stays. This is the separate question underneath it: does canopy *know* the
    two records are the same person?

    Until now it only knew implicitly — `(app, external_id)` sits on every
    contact row, so anything could join on it. Implicit is how two features end
    up computing "the same person" slightly differently and disagreeing, which
    is the `definition_key()` lesson from `apps/agents/definition.py`. One row,
    one answer.

    **This exposes nothing.** Every contact query stays scoped to its workspace;
    nothing reads across the link today. It exists so that combining a person's
    context across tenants can later be offered as a deliberate act, with
    whatever consent that turns out to need — rather than being impossible
    because the connection was never recorded at the moment it was knowable.

    Keyed on what the world ALREADY uses to name them, never on a guess:

    * a site's visitor is `(issuer, signer, external_id)` — that site's own id,
      in its own namespace, which cannot collide with another site's people;
    * a correspondent is their address, which for them IS the identity.

    A site asserting an email does NOT join those two. The site's id is the
    stronger key, and matching on an address it merely claims would be
    believing the assertion — the thing `auth_result` exists to avoid.
    """

    #: The SYSTEM that vouches for this person, when they came through one: the
    #: `iss` it signs with, and `AppCredential.signer()` — which keys it signs
    #: with. Not a row.
    #:
    #: It was an FK to the site row, when a site was one row shared by every
    #: tenant. Now each tenant registers a system itself (2026-09-24), so the
    #: same system is several rows and an FK would make one human arriving via
    #: two tenants two people — silently undoing this model. Two registrations
    #: that trust the same keys ARE the same signer, and only that signer can
    #: produce an assertion either of them accepts, so joining on it never
    #: merges two different systems. It can miss (one tenant pastes a key,
    #: another gives the JWKS URL), and a miss is the safe direction.
    issuer = models.CharField(max_length=100, blank=True, default="")
    signer = models.CharField(max_length=64, blank=True, default="")
    #: That site's own id for them. Opaque to canopy.
    external_id = models.CharField(max_length=200, blank=True, default="")
    #: Set when the address IS the identity (a correspondent), not when a site
    #: merely told us one.
    email = models.EmailField(blank=True, default="")
    #: The canopy ACCOUNT this person is, when they have one (fleet brain v1,
    #: canopy#804). A workspace member never has a `Contact` — so before this,
    #: the people agents talk to most had no `Person` at all and nothing could
    #: be remembered about them. Set by `services.person_for(user=…)`, which
    #: also joins the account to the correspondent row for its VERIFIED address
    #: (a login proves an address the way DMARC does), so someone who both
    #: emails an agent and talks to it in Slack is one person.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="person",
    )
    #: Agent memory is the PERSON's own, as two independent features (Jonathan,
    #: 2026-10-09: "each person should be able to turn it on or off"; then "it
    #: should be available per session, not just at the system level"):
    #:
    #: * record — "Agents may learn about me": agents may WRITE (HCP add / update /
    #:   delete, the legacy fact write) and are told to record.
    #: * use — "Agents may use what they've learned": agents may READ (HCP search /
    #:   get, and the facts served in the envelope's `person` block).
    #:
    #: Each has two canopy-level settings: `*_available` — may it be on at all —
    #: and `*_default` — is it on in a session that says nothing. A session may
    #: override either (`SessionAgentMemory`) but never turn on what is not
    #: available: effective = available AND (session override, else default). Both
    #: start unavailable for everyone. Record-only builds a model of the person that
    #: no agent uses yet. The person keeps full access to their own entries, export
    #: and audit log whatever these say, and turning one off deletes nothing. Only
    #: the person changes them (`PUT /api/people/me/agent-memory/`, and per session
    #: `PUT /api/people/me/sessions/{id}/agent-memory/`); every change is audited.
    hcp_record_available = models.BooleanField(default=False)
    hcp_record_default = models.BooleanField(default=False)
    hcp_record_changed_at = models.DateTimeField(null=True, blank=True)
    hcp_use_available = models.BooleanField(default=False)
    hcp_use_default = models.BooleanField(default=False)
    hcp_use_changed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contact_persons"
        verbose_name_plural = "people"
        constraints = [
            models.UniqueConstraint(
                fields=["issuer", "signer", "external_id"],
                condition=models.Q(external_id__gt=""),
                name="one_person_per_signer_visitor",
            ),
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(signer="") & models.Q(email__gt=""),
                name="one_person_per_correspondent",
            ),
        ]

    def __str__(self):
        return self.email or f"{self.issuer}:{self.external_id}"


class Contact(models.Model):
    """One person, as known to one workspace."""

    #: The PERSON this record is about, shared with every other tenant's record
    #: of the same human.
    #:
    #: Named `person`, not `identity`: `Contact.identity` is already a property
    #: returning how this contact is addressed, and a field of the same name is
    #: silently shadowed by it — Django never sees the field, so no column is
    #: ever created and every write is lost with no error.
    #:
    #: Nullable because not every contact has an identity canopy can key on —
    #: a Slack user has no address and no site id — and a null here must read
    #: as "we cannot tell", never as "the same as some other null".
    #:
    #: Nothing reads ACROSS this link today. Contact queries stay scoped to
    #: their workspace; this only records what was knowable at the moment the
    #: record was made, so combining a person's context across tenants can be
    #: offered later as a deliberate act rather than being impossible.
    person = models.ForeignKey("Person", on_delete=models.SET_NULL,
                               null=True, blank=True, related_name="contacts")
    #: How well this person's identity was established, on ONE ladder shared by
    #: every channel — see `email_auth.grade_of` for the mail side.
    #:
    #: A person can reach an agent by email or through an embedded widget, and
    #: both arrive as an ASSERTION by a third party: a mail server saying who
    #: sent a message, a host saying who its visitor is. They are the same kind
    #: of statement, so they are graded on the same scale rather than getting a
    #: second notion of trust that rules would then have to know about.
    #:
    #: The names are channel-specific labels on shared TIERS; `TIER_*` below
    #: are the tier-level constants a routing rule should use, so a rule reads
    #: "at least a signed assertion" instead of naming somebody else's channel.
    AUTH_NONE = "none"
    # Tier 1 — something authorised the delivery, but says nothing verifiable
    # about WHICH person.
    AUTH_SPF = "spf"
    AUTH_APP_SECRET = "app_secret"
    # Tier 2 — a signature covers this specific message or visitor.
    AUTH_DKIM = "dkim"
    AUTH_APP_SIGNED = "app_signed"
    # Slack signs every event it delivers, so the Slack ACCOUNT behind a
    # message is established by the platform — per message, like a DKIM
    # signature. What it does not establish is that the account's profile
    # email is the person's real-world identity, hence tier 2 and not 3.
    AUTH_SLACK = "slack"
    # Tier 3 — a FULL member of the Slack canopy is installed in: not a guest,
    # not a bot, not deactivated, and homed in that team (not a Slack Connect
    # visitor from someone else's org). That profile email was provisioned by
    # the organisation that owns the Slack, which is the same authority DMARC
    # leans on for mail — so it identifies the person the way an aligned
    # signature does. A guest or an outside team's member stays `slack` (tier 2):
    # their email is whatever THEIR org, or they, typed in.
    AUTH_SLACK_MEMBER = "slack_member"
    # Tier 3 — the signature is also tied to the identity the reader sees.
    AUTH_DMARC = "dmarc"
    # A DKIM signature BY the From: domain itself (`header.d`/`header.i` equal to
    # it). That is exactly what DMARC's DKIM-alignment check establishes; DMARC
    # only adds a published policy on top. It is what a domain that signs its
    # mail but publishes no DMARC record (dimagi-associate.com) can prove.
    AUTH_DKIM_ALIGNED = "dkim_aligned"
    # There is deliberately NO tier-3 grade for an embedded site. A host signs a
    # statement about its visitor, and canopy verifies the HOST's signature —
    # never the person behind it, which is what tier 3 means. The mint says the
    # same where it grades (`tokens/contact_api.py`: "it proves the SITE said
    # this, not that the human is who the site thinks").
    #
    # `app_signed_origin` ("Signed assertion from a framed origin") used to sit
    # here, ranked 3, and nothing ever assigned it — there is no path that could,
    # since a token is minted server-to-server with no browser origin in sight,
    # and a visitor canopy DOES resolve to an account stops being a contact
    # (`resolve_arrival`) and is graded on the user ladder instead. It was an
    # unearnable top grade that `:verified` rules were written against, so the
    # gap read as "this visitor failed the check" rather than "this check can
    # never pass". A stray stored value now ranks 0 via `AUTH_RANK.get(_, 0)`,
    # which is the fail-closed direction.
    AUTH_CHOICES = [
        (AUTH_NONE, "Unverified"),
        (AUTH_SPF, "SPF only (envelope sender)"),
        (AUTH_APP_SECRET, "App credential (proves the app, not the person)"),
        (AUTH_DKIM, "DKIM signed"),
        (AUTH_APP_SIGNED, "Signed assertion from the app"),
        (AUTH_SLACK, "Slack account (event signed by Slack)"),
        (AUTH_SLACK_MEMBER, "Member of our Slack (event signed by Slack)"),
        (AUTH_DMARC, "DMARC aligned"),
        (AUTH_DKIM_ALIGNED, "DKIM signed by the From: domain"),
    ]
    AUTH_RANK = {
        AUTH_NONE: 0,
        AUTH_SPF: 1, AUTH_APP_SECRET: 1,
        AUTH_DKIM: 2, AUTH_APP_SIGNED: 2, AUTH_SLACK: 2,
        AUTH_DMARC: 3, AUTH_DKIM_ALIGNED: 3, AUTH_SLACK_MEMBER: 3,
    }
    #: Tier-level aliases. Prefer these in a rule: `auth_at_least(TIER_SIGNED)`
    #: keeps working when a channel adds a label, where naming `AUTH_DKIM`
    #: quietly means "or anything an unrelated channel happens to rank 2".
    TIER_ASSERTED = AUTH_SPF          # 1
    TIER_SIGNED = AUTH_DKIM           # 2
    TIER_SIGNED_ALIGNED = AUTH_DMARC  # 3

    #: How canopy came to know this person.
    SOURCE_EMAIL = "email"
    SOURCE_EMBED = "embed"
    SOURCE_SLACK = "slack"
    SOURCE_CHOICES = [
        (SOURCE_EMAIL, "Wrote to an agent's inbox"),
        (SOURCE_EMBED, "Used an agent embedded in a connected site"),
        (SOURCE_SLACK, "Messaged an agent in the connected Slack"),
    ]

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="contacts",
        help_text="The tenant whose knowledge this is. A person dealt with by "
        "two workspaces has two contacts, deliberately — merging them would "
        "leak one tenant's dealings into another.",
    )
    source = models.CharField(
        max_length=8, choices=SOURCE_CHOICES, default=SOURCE_EMAIL,
        help_text="Which channel this person arrived through. Not cosmetic: it "
        "says which identity column is the real key, and therefore what a "
        "duplicate would even mean.",
    )
    email = models.EmailField(
        blank=True, default="",
        help_text="Lowercased. The identity as ASSERTED — see `auth_result` for "
        "how well it was backed up. BLANK is normal for a widget visitor: a "
        "host identifies its people by its own id and may never know an "
        "address, and demanding one would have forced a fake. On an embed "
        "contact this is a DESCRIPTION rather than a key: it is unique only "
        "where the email channel established it.",
    )

    #: The app that vouched for this person, for a contact that arrived through
    #: an embedded widget. Null for email contacts.
    #:
    #: `PROTECT` rather than `CASCADE`: disconnecting a site must not delete the
    #: record of everyone it introduced. That is the same reasoning the audit
    #: log uses — a trail that vanishes with its subject is not a trail — and
    #: the deliberate consequence is that a site with contacts cannot be hard
    #: deleted, only revoked, which is what `Disconnect` already does.
    app = models.ForeignKey(
        "tokens.AppCredential",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="contacts",
    )
    #: The host's OWN id for this person, opaque to canopy.
    #:
    #: Scoped by `app`, so it cannot collide with another host's ids or with
    #: canopy's users. That namespace isolation is why a host vouching for its
    #: own contacts is a strictly smaller grant than one asserting email
    #: addresses, which reach into canopy's user population.
    external_id = models.CharField(max_length=200, blank=True, default="")
    display_name = models.CharField(max_length=200, blank=True, default="")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_records",
        help_text="Set once this person has authenticated for real and become a "
        "canopy user. NULL is the normal state: most contacts are people an "
        "agent corresponds with and nothing more. Linking here still grants "
        "nothing — membership is a separate, explicit act.",
    )

    # --- what the mail server said ------------------------------------------
    auth_result = models.CharField(
        max_length=20, choices=AUTH_CHOICES, default=AUTH_NONE,
        help_text="The BEST grade seen from this address so far. Best rather "
        "than latest: one forwarded message that breaks SPF should not "
        "downgrade a correspondent, and a rule asking 'has this domain ever "
        "proved itself' wants the high-water mark.",
    )
    last_auth_result = models.CharField(
        max_length=20, choices=AUTH_CHOICES, default=AUTH_NONE,
        help_text="The grade on the most recent message. Kept beside the best "
        "one because a DROP is the interesting signal — a correspondent who "
        "always passed DMARC and suddenly does not is worth noticing.",
    )
    auth_detail = models.TextField(
        blank=True, default="",
        help_text="The raw Authentication-Results header the grade came from, "
        "for audit. Kept because a disputed grade is unanswerable without it.",
    )

    # --- what we know ---------------------------------------------------------
    notes = models.TextField(
        blank=True, default="",
        help_text="Free text a human or an agent has written about this person.",
    )
    attributes = models.JSONField(
        default=dict, blank=True,
        help_text="Structured facts, e.g. {'org': 'LLO Foo', 'connect_opp': 42}. "
        "A CACHE of what other systems say, never the authority: agent "
        "execution calls those systems and honours their ACLs in real time, so "
        "nothing here may be the thing that grants.",
    )

    #: Set to stop this person reaching an agent at all.
    #:
    #: A contact is the one principal an outsider can cause canopy to create —
    #: an inbound email or a host assertion is enough — so there has to be a way
    #: to say no to a specific person without disconnecting the whole site or
    #: closing the mailbox. Blocking grants nothing back either: it is a refusal
    #: at the door, checked wherever a contact would otherwise be acted on.
    blocked_at = models.DateTimeField(null=True, blank=True)
    blocked_reason = models.CharField(max_length=200, blank=True, default="")

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    message_count = models.PositiveIntegerField(
        default=0, help_text="Interactions attributed to this contact — inbound "
        "messages, and widget conversations started.",
    )

    class Meta:
        constraints = [
            # An address is unique only where it IS the identity, which is the
            # email channel. Two reasons, and the second is the one that bit:
            # every address-less widget contact would otherwise collide on the
            # empty string; and a widget contact carrying a host-asserted
            # address would collide with the real email contact for that
            # person, so recording what the host claimed would be impossible
            # exactly when it is most interesting.
            #
            # A widget contact's `email` is therefore a DESCRIPTION, not a key
            # — ungraded, host-supplied, and matching on it is precisely the
            # believing-the-assertion mistake the grade exists to prevent.
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=~models.Q(email="") & models.Q(source="email"),
                name="uniq_contact_per_workspace_email",
            ),
            # The host's own namespace. Scoped by app, so two sites may use the
            # same id for different people without meeting.
            models.UniqueConstraint(
                fields=["workspace", "app", "external_id"],
                condition=~models.Q(external_id=""),
                name="uniq_contact_per_app_external_id",
            ),
            # A Slack contact has no app, and NULLs never collide in a unique
            # index — so the constraint above would not stop two rows for one
            # Slack user. Keyed on `<team>:<user>`, Slack's own stable ids.
            models.UniqueConstraint(
                fields=["workspace", "external_id"],
                condition=models.Q(source="slack") & ~models.Q(external_id=""),
                name="uniq_contact_per_slack_user",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "last_seen_at"]),
            models.Index(fields=["app", "external_id"]),
        ]
        ordering = ["-last_seen_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.identity} @{self.workspace_id}"

    @property
    def identity(self) -> str:
        """Whatever canopy actually has to go on, for logs and display.

        An email contact has an address; a widget contact may only have the
        host's id. Falling back rather than showing a blank keeps a row
        identifiable in an audit view, which is where it matters most.
        """
        # Keyed on SOURCE, not on which column happens to be populated. An
        # embed contact may carry a host-asserted address, and showing that as
        # its identity would present an ungraded claim as though it were the
        # thing canopy matched on.
        if self.source == self.SOURCE_EMBED and self.external_id:
            return f"{self.app.name if self.app_id else '?'}:{self.external_id}"
        if self.email:
            return self.email
        return f"contact-{self.pk}"

    @property
    def is_blocked(self) -> bool:
        return self.blocked_at is not None

    #: Email grades that tie the signature to the visible From: — the ones that
    #: make THIS message "verified" (tier 3 on the mail side).
    EMAIL_ALIGNED = frozenset({AUTH_DMARC, AUTH_DKIM_ALIGNED})

    def auth_at_least(self, grade: str) -> bool:
        """Has this contact ever authenticated at `grade`'s tier or better?

        The comparison a routing rule wants. Reads the ladder off `AUTH_RANK`
        rather than spelling out a set per call site, so inserting a tier does
        not silently widen a rule that happened to enumerate its members.

        `grade` may be any label; only its TIER is compared, which is what lets
        one rule serve both channels. Prefer the `TIER_*` aliases at a call
        site — naming `AUTH_DKIM` reads as an email rule and would surprise
        whoever later finds it matching a widget visitor.
        """
        return self.AUTH_RANK.get(self.auth_result, 0) >= self.AUTH_RANK[grade]


# --- the fleet brain (canopy#804): what agents know about a person --------------
#
# Design: hal `docs/proposals/2026-10-07-caller-context-brain.md` §3, approved by
# Jonathan 2026-10-07. Two layers:
#
#   1. history — turns and messages, which already exist, keyed by initiator;
#   2. FACTS — `PersonFact`, append-only, small, one sentence each, recorded by
#      the session that learned them (HCP addPreference).
#
# (A third layer, a per-person DIGEST written by a separate agent turn, was
# removed 2026-10-09.) Every agent that serves a person reads (2) in its caller
# envelope (`apps/harness/caller_context.py`, envelope v3 `person`), and every
# such read is logged in `PersonAccess`, which the person can see. Raw
# conversations stay exactly as private as before: only facts cross agents.
#
# v1 serves facts ONLY within the workspace they were written in. Crossing a
# tenant is `Person`'s "deliberate act" and is not offered yet.


class PersonFact(models.Model):
    """One durable, work-context thing canopy knows about a person.

    **Append-only.** A fact is never edited: it is SUPERSEDED by a newer fact
    (`supersedes`), or RETRACTED (by the person, a workspace admin, or whoever
    asserted it). So every correction carries its own history, and "live" is
    simply "neither superseded nor retracted".

    **The kind list IS the privacy rule.** Only work context — no health, no
    personal life, no performance judgements or sentiment. A free-text
    "anything" kind is exactly what lets that creep, so `kind` is closed and
    enforced by a CHECK constraint, not only by the API.

    **Each row is one VERSION of an HCP entry** (Human Context Protocol v1,
    draft 4 — `apps/contacts/hcp.py`). Rows sharing `entry_id` are one entry's
    revision history: `version` counts up from 1, a new version supersedes the
    previous row, and the live row carries the entry's lifecycle `status`. The
    HCP `category` is the grant scope; it is limited to work-context categories
    by a CHECK constraint for the same reason `kind` is closed.
    """

    # --- HCP v1 (draft 4) -----------------------------------------------------
    #: Registered HCP categories canopy holds. The spec's others
    #: (health_context, purchase_history_preferences, values_and_ethics,
    #: location_context, education_learner_profile) are deliberately absent:
    #: they are not work context. Custom categories use `hcp-custom:`.
    CAT_GENERAL, CAT_GOALS = "general_preferences", "goals_and_constraints"
    CAT_WORK, CAT_COORDINATION = "work_context", "coordination_context"
    CATEGORIES = (CAT_GENERAL, CAT_GOALS, CAT_WORK, CAT_COORDINATION)
    CUSTOM_PREFIX = "hcp-custom:"

    ACTIVE, DEPRECATED, CONFLICTED, DELETED = "active", "deprecated", "conflicted", "deleted"
    STATUSES = (ACTIVE, DEPRECATED, CONFLICTED, DELETED)
    CONFIDENCES = ("high", "medium", "low")

    ROLE, PROJECT, INSTANCE = "role", "project", "instance"
    PREFERENCE, CORRECTION, TERMINOLOGY = "preference", "correction", "terminology"
    KIND_CHOICES = [
        (ROLE, "Role — who they are at work"),
        (PROJECT, "Project — work they are part of"),
        (INSTANCE, "Instance — a specific thing they work with (a bot, an app, a report)"),
        (PREFERENCE, "Preference — how they like to work with agents"),
        (CORRECTION, "Correction — something an agent got wrong, to honour"),
        (TERMINOLOGY, "Terminology — the words they use for things"),
    ]
    KINDS = frozenset(k for k, _ in KIND_CHOICES)

    # HCP `declarationType`: declared ↔ user-declared, inferred ↔ model-inferred,
    # attested ↔ issuer-attested (a human other than the person asserted it —
    # an admin's note about a contact, an integration).
    DECLARED, INFERRED, ATTESTED = "declared", "inferred", "attested"
    BASIS_CHOICES = [
        (DECLARED, "Declared — the person said it"),
        (INFERRED, "Inferred — a model concluded it"),
        (ATTESTED, "Attested — someone other than the person asserted it"),
    ]
    BASES = frozenset(b for b, _ in BASIS_CHOICES)

    STATEMENT_MAX = 500

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="facts")
    #: Where it was written. v1 serves a fact to an AGENT only inside this
    #: workspace. NULL = a PERSONAL entry: one the person wrote about themself,
    #: or one a client they authorized over OAuth wrote (docs/architecture/
    #: hcp-service.md). No workspace's agents read a personal entry; the person
    #: and the OAuth clients they grant do.
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  null=True, blank=True, related_name="person_facts")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    statement = models.CharField(max_length=STATEMENT_MAX)
    basis = models.CharField(max_length=10, choices=BASIS_CHOICES, default=DECLARED)
    project = models.ForeignKey("agents.AgentProject", on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="person_facts")
    #: A specific instance in words, e.g. "OCS bot 'KMC Audit' (team Vaccine_Coach)".
    instance_ref = models.CharField(max_length=300, blank=True, default="")
    #: The turn it came from. Only people who can already read that turn can
    #: open it; the link itself widens nothing.
    source_turn = models.ForeignKey("harness.Turn", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="person_facts")
    asserted_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                         null=True, blank=True, related_name="person_facts_asserted")
    asserted_by_agent = models.ForeignKey("agents.Agent", on_delete=models.SET_NULL,
                                          null=True, blank=True, related_name="person_facts_asserted")
    supersedes = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="superseded_by")
    superseded_at = models.DateTimeField(null=True, blank=True)
    retracted_at = models.DateTimeField(null=True, blank=True)
    retracted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="person_facts_retracted")
    #: Set on the fact that MIRRORS a contact's free-text `notes` (v1.1): the
    #: one live mirrored fact per contact is the one with this set, which is
    #: what makes the backfill (contacts/0011) idempotent and lets an edit to the notes
    #: supersede it (`people.mirror_contact_notes`).
    source_contact = models.ForeignKey("contacts.Contact", on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name="mirrored_facts")
    created_at = models.DateTimeField(auto_now_add=True)

    # --- HCP v1 entry fields ----------------------------------------------------
    #: The HCP entry id (`urn:uuid:<entry_id>`) — the same on every version.
    entry_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    version = models.PositiveIntegerField(default=1)
    category = models.CharField(max_length=80, default=CAT_WORK)
    #: Free-form, scoped within `category`; same category + normalized
    #: dimension makes an inferred and a declared entry conflict candidates.
    dimension = models.CharField(max_length=120, blank=True, default="")
    #: high | medium | low — present iff `basis` is inferred.
    confidence = models.CharField(max_length=8, blank=True, default="")
    status = models.CharField(max_length=12, default=ACTIVE)
    #: Has the person confirmed this entry themself?
    user_verified = models.BooleanField(default=False)
    #: `record.provenance.source` — "turn:<id>", "user-input", "integration:…".
    provenance_source = models.CharField(max_length=200, blank=True, default="")
    #: `record.provenance.capturedBy` — the agent, and for an inference the model.
    captured_by = models.CharField(max_length=200, blank=True, default="")
    #: `claim.expirationDate`; past it the entry is not served.
    expires_at = models.DateTimeField(null=True, blank=True)
    #: `claim.subject.relationship` — reserved; confers nothing (HCP 2.2).
    relationship = models.CharField(max_length=80, blank=True, default="")
    #: `record.metadata` — free-form; never affects scope or authorization.
    metadata = models.JSONField(default=dict, blank=True)
    #: Why this version exists (the update/delete `reason`).
    reason = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        db_table = "contact_person_facts"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["person", "workspace", "created_at"]),
                   models.Index(fields=["person", "workspace", "category", "status"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind__in=["role", "project", "instance", "preference",
                                             "correction", "terminology"]),
                name="person_fact_kind_is_work_context",
            ),
            models.CheckConstraint(
                condition=models.Q(basis__in=["declared", "inferred", "attested"]),
                name="person_fact_basis_known_v2",
            ),
            models.CheckConstraint(
                condition=(models.Q(category__in=["general_preferences", "goals_and_constraints",
                                                  "work_context", "coordination_context"])
                           | models.Q(category__startswith="hcp-custom:")),
                name="person_fact_category_is_work_context",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["active", "deprecated", "conflicted", "deleted"]),
                name="person_fact_status_known",
            ),
            models.CheckConstraint(
                condition=(models.Q(basis="inferred", confidence__in=["high", "medium", "low"])
                           | (~models.Q(basis="inferred") & models.Q(confidence=""))),
                name="person_fact_confidence_iff_inferred",
            ),
            models.UniqueConstraint(fields=["entry_id", "version"],
                                    name="person_fact_one_row_per_entry_version"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.person_id}@{self.workspace_id} {self.kind}: {self.statement[:40]}"

    @property
    def is_live(self) -> bool:
        """The entry's current version, not deleted. A CONFLICTED entry is live
        (the person sees it) but is never served to an agent — see `servable`."""
        return self.superseded_at is None and self.retracted_at is None

    @property
    def is_current(self) -> bool:
        """The entry's latest version (whatever its status)."""
        return self.superseded_at is None


class PersonAccess(models.Model):
    """Append-only audit: who READ what canopy knows about a person, and how.

    The person sees the last of these on "What agents know about me", which is
    what makes it safe for an agent to quote its facts back: transparency
    instead of secrecy.
    """

    VIA_ENVELOPE, VIA_API = "envelope", "api"
    VIA_CHOICES = [(VIA_ENVELOPE, "In an agent turn's caller envelope"),
                   (VIA_API, "Through the REST/MCP API")]

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="accesses")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  null=True, blank=True, related_name="person_accesses")
    reader_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="person_reads")
    reader_agent = models.ForeignKey("agents.Agent", on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="person_reads")
    turn = models.ForeignKey("harness.Turn", on_delete=models.SET_NULL,
                             null=True, blank=True, related_name="person_reads")
    via = models.CharField(max_length=10, choices=VIA_CHOICES)
    #: For an envelope read: did the `person` block carry anything — at least
    #: one live fact — at the moment it was BUILT. The
    #: coverage metric (`people_coverage`) counts human turns whose envelope
    #: had context from this, rather than re-deriving it later from facts that
    #: have since changed. False for API reads (not meaningful there).
    had_context = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contact_person_accesses"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["person", "created_at"])]


# --- HCP v1 grants and the person's audit log (apps/contacts/hcp.py) -------------


class PersonGrant(models.Model):
    """HCP 4.1.5 — one client's category-scoped, revocable access to a person's
    entries.

    **A first-party client is the agent** (Jonathan, 2026-10-09: "each agent
    session / entry should obtain the grant explicitly or due to previous
    granting to this agent"). Its `client_key` names the agent only; a
    "for this session" grant adds the session to `attributes`. The channel/host
    columns stay for older rows. An OAuth app's grant is keyed on `hcp_client`.

    **Only ever the person's act** (4.1.6): given in a session's UI after the
    agent, categories, actions and duration are shown (`hcp.issue_agent_grant`,
    `modality=canopy-chat|canopy-widget`), or through the HCP service's consent
    screen. Nothing is presumed; the control plane's presumed grants
    (`canopy-control-plane`) were retired by migration 0022. A revoked grant is
    never revived: only a new act gives a new one.
    """

    TEMPORARY, PERSISTENT = "temporary", "persistent"
    ACTIVE, REVOKED, EXPIRED = "active", "revoked", "expired"
    MODALITY_CONTROL_PLANE = "canopy-control-plane"

    grant_id = models.UUIDField(default=uuid.uuid4, unique=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="grants")
    #: The workspace whose entries the grant reaches (an agent serves its own).
    #: NULL for a grant to an OAuth client (`hcp_client`), whose reach is the
    #: person's personal entries plus any workspaces they chose, recorded as a
    #: restriction (4.1.5.1).
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  null=True, blank=True, related_name="person_grants")
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, null=True, blank=True,
                              related_name="person_grants")
    #: The OAuth client this grant was issued to over the HCP service's
    #: authorization-code flow (docs/architecture/hcp-service.md). NULL for a
    #: first-party agent grant (the `agent`/`channel`/`host` key above).
    hcp_client = models.ForeignKey("contacts.HcpClient", on_delete=models.CASCADE, null=True,
                                   blank=True, related_name="grants")
    #: slack | email | chat | web | mcp | api | widget | sdk | … ("" = any).
    channel = models.CharField(max_length=40, blank=True, default="")
    #: The embedding host (a connected site) when the channel is an SDK/widget.
    host = models.CharField(max_length=200, blank=True, default="")
    #: Further client dimensions, reserved — part of `client_key` when present.
    attributes = models.JSONField(default=dict, blank=True)
    client_key = models.CharField(max_length=400, db_index=True)
    client_name = models.CharField(max_length=200)
    scopes = models.JSONField(default=list)
    restrictions = models.JSONField(default=list, blank=True)
    grant_type = models.CharField(max_length=12, default=PERSISTENT)
    #: Who authorized it when not the person (HCP 4.1.5 `grantor`); "" = the person.
    grantor = models.CharField(max_length=200, blank=True, default="")
    modality = models.CharField(max_length=40, default=MODALITY_CONTROL_PLANE)
    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, default=ACTIVE)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "contact_person_grants"
        ordering = ["-issued_at"]
        indexes = [models.Index(fields=["person", "client_key", "issued_at"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(status__in=["active", "revoked", "expired"]),
                                   name="person_grant_status_known"),
            models.CheckConstraint(condition=models.Q(grant_type__in=["temporary", "persistent"]),
                                   name="person_grant_type_known"),
            # 4.1.4: a temporary grant always has an absolute expiry.
            models.CheckConstraint(
                condition=~models.Q(grant_type="temporary") | models.Q(expires_at__isnull=False),
                name="person_grant_temporary_expires"),
        ]


class PersonAuditEvent(models.Model):
    """HCP 4.3 — the PERSON's audit log: append-only, theirs to read and export,
    never readable by an agent. Holds identifiers and categories, never entry
    content, so it survives a hard delete without keeping what was deleted."""

    EVENT_TYPES = (
        "preference.created", "preference.read", "preference.updated", "preference.deleted",
        "preference.hardDeleted", "preference.exported", "grant.issued", "grant.revoked",
        "grant.expired", "conflict.detected", "conflict.resolved", "revocation.notified",
        # canopy's own, beyond the spec's required set (4.3.1 lists what MUST be
        # logged, not all that may be): the person turned one of their two
        # agent-memory settings. `agentX.enabled|disabled` = the feature made
        # available / unavailable (`Person.hcp_*_available`); `agentX.defaultOn|
        # defaultOff` = its default for sessions (`hcp_*_default`); `sessionX.on|off|
        # inherit` = one session's override (`SessionAgentMemory`).
        "agentRecord.enabled", "agentRecord.disabled", "agentUse.enabled", "agentUse.disabled",
        "agentRecord.defaultOn", "agentRecord.defaultOff",
        "agentUse.defaultOn", "agentUse.defaultOff",
        "sessionRecord.on", "sessionRecord.off", "sessionRecord.inherit",
        "sessionUse.on", "sessionUse.off", "sessionUse.inherit",
    )
    USER, AGENT, SYSTEM = "user", "agent", "system"

    event_id = models.UUIDField(default=uuid.uuid4, unique=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="audit_events")
    event_type = models.CharField(max_length=32)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    actor_id = models.CharField(max_length=200)
    actor_type = models.CharField(max_length=8)
    entry_id = models.UUIDField(null=True, blank=True)
    related_entry_id = models.UUIDField(null=True, blank=True)
    category = models.CharField(max_length=80, blank=True, default="")
    purpose = models.CharField(max_length=500, blank=True, default="")
    grant_id = models.UUIDField(null=True, blank=True)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name="person_audit_events")
    turn = models.ForeignKey("harness.Turn", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="person_audit_events")
    detail = models.TextField(blank=True, default="")

    class Meta:
        db_table = "contact_person_audit_events"
        ordering = ["-timestamp", "-pk"]
        indexes = [models.Index(fields=["person", "timestamp"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(actor_type__in=["user", "agent", "system"]),
                                   name="person_audit_actor_type_known"),
        ]

    def save(self, *args, **kwargs):
        # Append-only (HCP 4.3): a row is written once and never changed.
        if self.pk is not None and not kwargs.pop("_allow_update", False):
            raise ValueError("PersonAuditEvent is append-only")
        super().save(*args, **kwargs)


class SessionAgentMemory(models.Model):
    """One session's override of a person's agent-memory defaults (Jonathan,
    2026-10-09: "in any given session, you can provide yes to either if they are
    enabled at the canopy level"). `None` = inherit the person's default. It can
    never turn on a feature the person has not made available — `hcp.effective`
    applies `available AND (override, else default)`. Keyed by (session, person) so
    in a shared session one person's choice never speaks for another; only the
    person sets it, and each change is on their audit log."""

    session = models.ForeignKey("canopy_sessions.Session", on_delete=models.CASCADE,
                                related_name="agent_memory")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="session_memory")
    record = models.BooleanField(null=True, blank=True)
    use = models.BooleanField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "contact_session_agent_memory"
        constraints = [models.UniqueConstraint(fields=["session", "person"],
                                               name="session_agent_memory_once")]


class HcpIdempotencyKey(models.Model):
    """HCP 3.4.3 — a POST/PUT replayed with the same key returns the original
    response and does not run (or audit) twice. Kept ≥24h."""

    key = models.CharField(max_length=200)
    #: Who replayed it: a canopy account, or (HCP service) an OAuth client.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True,
                             blank=True, related_name="hcp_idempotency_keys")
    client = models.ForeignKey("contacts.HcpClient", on_delete=models.CASCADE, null=True,
                               blank=True, related_name="idempotency_keys")
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=300)
    body_hash = models.CharField(max_length=64)
    status = models.PositiveSmallIntegerField()
    response = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "contact_hcp_idempotency_keys"
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], condition=models.Q(user__isnull=False),
                                    name="hcp_idempotency_key_per_user"),
            models.UniqueConstraint(fields=["client", "key"], condition=models.Q(client__isnull=False),
                                    name="hcp_idempotency_key_per_client"),
        ]


# --- the HCP service: OAuth clients outside canopy (docs/architecture/hcp-service.md) ---


class HcpClient(models.Model):
    """An application canopy does not operate that may ask a person for access to
    their HCP instance (HCP 5.2.1: the reason the service is `oauth2`).

    Registered by a canopy admin (superuser) only — there is no dynamic
    registration. A public client: it holds no secret and proves possession of
    each authorization code with PKCE S256. Disabling a client stops every token
    it holds at once; the grants stay on the person's list, inert, until they
    revoke them."""

    client_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    #: Who runs it — shown to the person on the consent screen next to the name.
    operator = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True, default="")
    #: Exact-match redirect URIs (no prefix or wildcard matching).
    redirect_uris = models.JSONField(default=list)
    #: The most it may ever ask for: `hcp:{category}:{read|write}` scopes.
    allowed_scopes = models.JSONField(default=list)
    #: Operated by Dimagi (canopy's own tooling) rather than a third party. Only
    #: shown to the person; it grants nothing extra.
    first_party = models.BooleanField(default=False)
    #: HCP 4.2.3 revocation notifications: where to POST, and the HMAC key
    #: (encrypted at rest with apps.common.encryption; shown to the admin once).
    webhook_url = models.URLField(max_length=500, blank=True, default="")
    webhook_secret_encrypted = models.TextField(blank=True, default="")
    registered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="hcp_clients_registered")
    created_at = models.DateTimeField(auto_now_add=True)
    disabled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "contact_hcp_clients"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.client_id})"

    @property
    def is_active(self) -> bool:
        return self.disabled_at is None


class HcpAuthorizationCode(models.Model):
    """An authorization code (RFC 6749 4.1.2): single use, ten minutes, bound to
    the client, the redirect URI and the PKCE challenge, and to the grant the
    person just created."""

    code_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(HcpClient, on_delete=models.CASCADE, related_name="codes")
    grant = models.ForeignKey(PersonGrant, on_delete=models.CASCADE, related_name="hcp_codes")
    redirect_uri = models.CharField(max_length=500)
    code_challenge = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contact_hcp_codes"


class HcpToken(models.Model):
    """An access or refresh token issued to an OAuth client under one grant.
    Stored as a sha256 of the raw value. Revoking the grant revokes every token
    (HCP 4.2.2); refresh tokens rotate, and a rotated one presented again revokes
    the grant (OAuth 2.1 reuse detection)."""

    ACCESS, REFRESH = "access", "refresh"

    token_hash = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=8)
    client = models.ForeignKey(HcpClient, on_delete=models.CASCADE, related_name="tokens")
    grant = models.ForeignKey(PersonGrant, on_delete=models.CASCADE, related_name="hcp_tokens")
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    #: A refresh token's single use (rotation); set when it is exchanged.
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "contact_hcp_tokens"
        indexes = [models.Index(fields=["grant", "kind"])]
        constraints = [models.CheckConstraint(condition=models.Q(kind__in=["access", "refresh"]),
                                              name="hcp_token_kind_known")]


class HcpRevocationDelivery(models.Model):
    """One revocation notification owed to a client's webhook (HCP 4.2.3):
    retried with backoff for at least 5 attempts over at least an hour, every
    attempt the same payload under a fresh HCP-Delivery-Id. Revocation itself
    never waits for it."""

    grant = models.ForeignKey(PersonGrant, on_delete=models.CASCADE, related_name="revocation_deliveries")
    client = models.ForeignKey(HcpClient, on_delete=models.CASCADE, related_name="revocation_deliveries")
    payload = models.JSONField()
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(db_index=True)
    last_status = models.CharField(max_length=200, blank=True, default="")
    delivered_at = models.DateTimeField(null=True, blank=True)
    abandoned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contact_hcp_revocation_deliveries"
