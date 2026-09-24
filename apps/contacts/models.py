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

    * a site's visitor is `(app, external_id)` — that site's own id, in its own
      namespace, which cannot collide with another site's people;
    * a correspondent is their address, which for them IS the identity.

    A site asserting an email does NOT join those two. The site's id is the
    stronger key, and matching on an address it merely claims would be
    believing the assertion — the thing `auth_result` exists to avoid.
    """

    #: The site that vouches for this person, when they came through one.
    app = models.ForeignKey("tokens.AppCredential", on_delete=models.CASCADE,
                            null=True, blank=True, related_name="contact_identities")
    #: That site's own id for them. Opaque to canopy.
    external_id = models.CharField(max_length=200, blank=True, default="")
    #: Set when the address IS the identity (a correspondent), not when a site
    #: merely told us one.
    email = models.EmailField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contact_persons"
        verbose_name_plural = "people"
        constraints = [
            models.UniqueConstraint(
                fields=["app", "external_id"],
                condition=models.Q(external_id__gt=""),
                name="one_person_per_site_visitor",
            ),
            models.UniqueConstraint(
                fields=["email"],
                condition=models.Q(app__isnull=True) & models.Q(email__gt=""),
                name="one_person_per_correspondent",
            ),
        ]

    def __str__(self):
        return self.email or f"{self.app_id}:{self.external_id}"


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
        (AUTH_DMARC, "DMARC aligned"),
        (AUTH_DKIM_ALIGNED, "DKIM signed by the From: domain"),
    ]
    AUTH_RANK = {
        AUTH_NONE: 0,
        AUTH_SPF: 1, AUTH_APP_SECRET: 1,
        AUTH_DKIM: 2, AUTH_APP_SIGNED: 2, AUTH_SLACK: 2,
        AUTH_DMARC: 3, AUTH_DKIM_ALIGNED: 3,
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
