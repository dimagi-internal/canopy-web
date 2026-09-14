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


class Contact(models.Model):
    """One person, as known to one workspace."""

    #: Ordered weakest to strongest — see `email_auth.grade_of`. Stored as the
    #: grade rather than the raw header so a routing rule can compare tiers
    #: without re-parsing, and kept as an ordered list so "at least DKIM" is
    #: expressible.
    AUTH_NONE = "none"
    AUTH_SPF = "spf"
    AUTH_DKIM = "dkim"
    AUTH_DMARC = "dmarc"
    AUTH_CHOICES = [
        (AUTH_NONE, "Unverified"),
        (AUTH_SPF, "SPF only (envelope sender)"),
        (AUTH_DKIM, "DKIM signed"),
        (AUTH_DMARC, "DMARC aligned"),
    ]
    AUTH_RANK = {AUTH_NONE: 0, AUTH_SPF: 1, AUTH_DKIM: 2, AUTH_DMARC: 3}

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="contacts",
        help_text="The tenant whose knowledge this is. A person dealt with by "
        "two workspaces has two contacts, deliberately — merging them would "
        "leak one tenant's dealings into another.",
    )
    email = models.EmailField(
        help_text="Lowercased. The identity as ASSERTED — see `auth_result` for "
        "whether the sending domain backed it up.",
    )
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
        max_length=8, choices=AUTH_CHOICES, default=AUTH_NONE,
        help_text="The BEST grade seen from this address so far. Best rather "
        "than latest: one forwarded message that breaks SPF should not "
        "downgrade a correspondent, and a rule asking 'has this domain ever "
        "proved itself' wants the high-water mark.",
    )
    last_auth_result = models.CharField(
        max_length=8, choices=AUTH_CHOICES, default=AUTH_NONE,
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

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    message_count = models.PositiveIntegerField(
        default=0, help_text="Inbound messages attributed to this contact.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "email"], name="uniq_contact_per_workspace_email",
            ),
        ]
        indexes = [models.Index(fields=["workspace", "last_seen_at"])]
        ordering = ["-last_seen_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.email} @{self.workspace_id}"

    def auth_at_least(self, grade: str) -> bool:
        """Has this contact ever authenticated at `grade` or better?

        The comparison a routing rule wants. Reads the ladder off `AUTH_RANK`
        rather than spelling out a set per call site, so inserting a tier does
        not silently widen a rule that happened to enumerate its members.
        """
        return self.AUTH_RANK.get(self.auth_result, 0) >= self.AUTH_RANK[grade]
