"""A contact who arrived through a widget, not a mailbox.

`Contact` was built for the inbound-email case, where the identity IS an
address. A widget visitor may have no address at all — the host knows them by
its own id — so the same record has to work without one, and the assertion the
host makes has to be graded on the same ladder as a mail server's rather than
getting a second notion of trust.

What must not change is the property the model exists for: recording somebody
grants nothing.
"""

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from apps.contacts import services
from apps.contacts.models import Contact
from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.site_tenant import host_workspace

pytestmark = pytest.mark.django_db


def _ws(slug="w1"):
    owner = User.objects.create_user(f"o-{slug}", f"o-{slug}@dimagi.com", "pw")
    ws = Workspace.objects.create(slug=slug, display_name=slug, created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


def _app(name="connect-labs"):
    _raw, app = AppCredential.create_credential(name=name, created_by=None,
                                                workspace=host_workspace())
    return app


# --- identity without an address ----------------------------------------------


def test_a_widget_visitor_is_recorded_with_no_email_at_all():
    """A host identifies its people by its own id and may never know an
    address. Demanding one would have forced a fake."""
    ws, app = _ws(), _app()

    c = services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")

    assert c is not None
    assert c.email == ""
    assert c.external_id == "u-42"
    assert c.source == Contact.SOURCE_EMBED
    assert c.identity == "connect-labs:u-42"


def test_two_widget_visitors_with_no_email_can_coexist():
    """The old unique constraint was on (workspace, email); unconditional, it
    would collide every address-less contact on the empty string and let only
    the first exist."""
    ws, app = _ws(), _app()

    services.record_embed_visitor(workspace=ws, app=app, external_id="a")
    services.record_embed_visitor(workspace=ws, app=app, external_id="b")

    assert Contact.objects.filter(workspace=ws).count() == 2


def test_the_same_id_from_the_same_app_is_the_same_person():
    ws, app = _ws(), _app()

    first = services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")
    again = services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")

    assert first.pk == again.pk
    assert again.message_count == 2


def test_the_same_id_from_a_DIFFERENT_app_is_a_different_person():
    """The namespace isolation that makes this a smaller grant than asserting
    emails: one host's `u-42` is not another's."""
    ws = _ws()
    a, b = _app("connect-labs"), _app("other-site")

    services.record_embed_visitor(workspace=ws, app=a, external_id="u-42")
    services.record_embed_visitor(workspace=ws, app=b, external_id="u-42")

    assert Contact.objects.count() == 2


def test_a_host_supplied_email_does_not_merge_with_an_email_contact():
    """Matching on the address would be BELIEVING the assertion, which is the
    one thing the grade exists to avoid. Linking is a deliberate act."""
    ws, app = _ws(), _app()
    services.record_inbound_sender(workspace=ws, address="p@llo.org")

    services.record_embed_visitor(
        workspace=ws, app=app, external_id="u-42", email="p@llo.org"
    )

    assert Contact.objects.filter(workspace=ws).count() == 2


@pytest.mark.parametrize("other_channel", ["embed", "slack"])
def test_the_next_email_still_finds_its_own_contact(other_channel):
    """The two rows above must not collide on the NEXT email either. The email
    lookup matched on (workspace, email) alone, so a Slack or widget contact
    carrying the same address made it raise MultipleObjectsReturned — the turn
    lost its sender, ran as `unknown`, and an agent with a declared interface
    refused its own owner's mail (ace, 2026-09-24)."""
    ws = _ws()
    first = services.record_inbound_sender(workspace=ws, address="p@llo.org")
    if other_channel == "embed":
        services.record_embed_visitor(
            workspace=ws, app=_app(), external_id="u-42", email="p@llo.org"
        )
    else:
        services.record_slack_user(
            workspace=ws, team_id="T1", slack_user_id="U1", email="p@llo.org"
        )

    again = services.record_inbound_sender(workspace=ws, address="p@llo.org")

    assert again.pk == first.pk
    assert again.source == Contact.SOURCE_EMAIL


def test_an_id_less_visitor_is_refused_rather_than_recorded_unmatchable():
    ws, app = _ws(), _app()
    assert services.record_embed_visitor(workspace=ws, app=app, external_id="  ") is None
    assert not Contact.objects.exists()


# --- one ladder, two channels -------------------------------------------------


def test_a_host_assertion_is_graded_on_the_same_ladder_as_an_email():
    ws, app = _ws(), _app()

    weak = services.record_embed_visitor(workspace=ws, app=app, external_id="a")
    strong = services.record_embed_visitor(
        workspace=ws, app=app, external_id="b", grade=Contact.AUTH_APP_SIGNED
    )

    # A shared secret proves the APP, not the person — the same tier as SPF.
    assert weak.auth_result == Contact.AUTH_APP_SECRET
    assert weak.auth_at_least(Contact.TIER_ASSERTED)
    assert not weak.auth_at_least(Contact.TIER_SIGNED)
    # A signed per-visitor assertion is the DKIM tier.
    assert strong.auth_at_least(Contact.TIER_SIGNED)


def test_a_rule_written_for_one_channel_reads_the_tier_not_the_channel():
    """`auth_at_least` compares tiers, which is what lets one routing rule serve
    both. Pinned because the alternative — a rule enumerating channel labels —
    silently stops matching when a channel is added."""
    ws, app = _ws(), _app()
    c = services.record_embed_visitor(
        workspace=ws, app=app, external_id="a", grade=Contact.AUTH_APP_SIGNED
    )

    # Asking in email vocabulary still works, because only the tier is compared.
    assert c.auth_at_least(Contact.AUTH_DKIM)
    assert not c.auth_at_least(Contact.AUTH_DMARC)


def test_the_grade_is_still_a_high_water_mark():
    ws, app = _ws(), _app()
    services.record_embed_visitor(
        workspace=ws, app=app, external_id="a", grade=Contact.AUTH_APP_SIGNED
    )

    c = services.record_embed_visitor(
        workspace=ws, app=app, external_id="a", grade=Contact.AUTH_APP_SECRET
    )

    assert c.auth_result == Contact.AUTH_APP_SIGNED   # best ever
    assert c.last_auth_result == Contact.AUTH_APP_SECRET  # and the drop is visible


def test_an_unknown_grade_falls_to_none_rather_than_being_stored():
    ws, app = _ws(), _app()
    c = services.record_embed_visitor(
        workspace=ws, app=app, external_id="a", grade="totally-trustworthy"
    )
    assert c.auth_result == Contact.AUTH_NONE


# --- a host must not be able to relabel someone -------------------------------


def test_a_later_visit_cannot_overwrite_an_established_name_or_address():
    """Both are host-supplied on every visit. Letting a later one win would let
    a host silently re-address an established contact."""
    ws, app = _ws(), _app()
    services.record_embed_visitor(
        workspace=ws, app=app, external_id="a", display_name="Real Name", email="real@llo.org"
    )

    c = services.record_embed_visitor(
        workspace=ws, app=app, external_id="a", display_name="Someone Else", email="evil@x.test"
    )

    assert c.display_name == "Real Name"
    assert c.email == "real@llo.org"


def test_but_a_blank_is_filled_in_later():
    ws, app = _ws(), _app()
    services.record_embed_visitor(workspace=ws, app=app, external_id="a")

    c = services.record_embed_visitor(
        workspace=ws, app=app, external_id="a", display_name="Later Known"
    )

    assert c.display_name == "Later Known"


# --- still not a grant --------------------------------------------------------


def test_recording_a_widget_visitor_grants_nothing():
    """The property the whole model exists for, asserted for the new channel."""
    ws, app = _ws(), _app()

    c = services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")

    assert c.user is None
    assert not WorkspaceMembership.objects.filter(workspace=ws).exclude(
        user__username="o-w1"
    ).exists()


# --- blocking -----------------------------------------------------------------


def test_a_single_person_can_be_refused_without_disconnecting_the_site():
    ws, app = _ws(), _app()
    c = services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")

    services.block(c, reason="abuse")

    c.refresh_from_db()
    assert c.is_blocked and c.blocked_reason == "abuse"
    assert app.revoked_at is None, "blocking one person must not revoke the site"


def test_blocking_keeps_the_record():
    """What canopy knows about someone it has decided to refuse is exactly what
    it should not forget."""
    ws, app = _ws(), _app()
    c = services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")
    services.block(c)
    assert Contact.objects.filter(pk=c.pk).exists()
    services.unblock(c)
    c.refresh_from_db()
    assert not c.is_blocked


# --- the email path is untouched ----------------------------------------------


def test_email_contacts_still_behave_exactly_as_before():
    ws = _ws()
    c = services.record_inbound_sender(workspace=ws, address="P@LLO.org", display_name="P")
    assert c.email == "p@llo.org"
    assert c.source == Contact.SOURCE_EMAIL
    assert c.app_id is None
    assert c.identity == "p@llo.org"


def test_two_email_contacts_with_the_same_address_still_cannot_exist():
    """The constraint became conditional, not absent."""
    ws = _ws()
    services.record_inbound_sender(workspace=ws, address="p@llo.org")
    with pytest.raises(IntegrityError), transaction.atomic():
        Contact.objects.create(workspace=ws, email="p@llo.org")


def test_two_widget_contacts_with_the_same_id_still_cannot_exist():
    ws, app = _ws(), _app()
    services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")
    with pytest.raises(IntegrityError), transaction.atomic():
        Contact.objects.create(workspace=ws, app=app, external_id="u-42")


def test_disconnecting_a_site_does_not_delete_the_people_it_introduced():
    """`PROTECT`, deliberately: a trail that vanishes with its subject is not a
    trail. The consequence is that a site with contacts is revoked, not deleted
    — which is what Disconnect already does."""
    from django.db.models import ProtectedError

    ws, app = _ws(), _app()
    services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")

    with pytest.raises(ProtectedError):
        app.delete()


# --- the API surface ----------------------------------------------------------


def _client(ws, role=WorkspaceMembership.EDITOR):
    from django.test import Client

    user = User.objects.create_user(f"m-{role}", f"m-{role}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=role)
    c = Client()
    c.force_login(user)
    return c


def test_the_list_says_which_channel_and_what_it_matched_on():
    ws, app = _ws(), _app()
    services.record_embed_visitor(
        workspace=ws, app=app, external_id="u-42", email="claimed@llo.org"
    )
    c = _client(ws)

    row = c.get("/api/contacts/").json()["items"][0]

    assert row["source"] == "embed"
    assert row["external_id"] == "u-42"
    assert row["app_name"] == "connect-labs"
    # Identity is the KEY, not the host's claim about their address — showing
    # the claim here would present it as the thing canopy matched on.
    assert row["identity"] == "connect-labs:u-42"
    assert row["email"] == "claimed@llo.org"


def test_a_person_can_be_blocked_and_unblocked_over_http():
    ws, app = _ws(), _app()
    contact = services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")
    c = _client(ws)

    r = c.patch(f"/api/contacts/{contact.pk}/",
                data={"blocked": True, "blocked_reason": "abuse"},
                content_type="application/json")

    assert r.status_code == 200, r.content
    assert r.json()["is_blocked"] is True
    assert r.json()["blocked_reason"] == "abuse"

    r = c.patch(f"/api/contacts/{contact.pk}/", data={"blocked": False},
                content_type="application/json")
    assert r.json()["is_blocked"] is False


def test_blocking_goes_through_the_service_not_a_bare_setattr():
    """`blocked` is a verb, not a column — it sets a timestamp and a reason
    together. A generic `setattr` loop would set a bogus attribute and then
    hand `update_fields` a name the model does not have."""
    ws, app = _ws(), _app()
    contact = services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")
    c = _client(ws)

    c.patch(f"/api/contacts/{contact.pk}/", data={"blocked": True},
            content_type="application/json")

    contact.refresh_from_db()
    assert contact.blocked_at is not None


def test_a_viewer_cannot_block_anyone():
    ws, app = _ws(), _app()
    contact = services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")
    c = _client(ws, role=WorkspaceMembership.VIEWER)

    r = c.patch(f"/api/contacts/{contact.pk}/", data={"blocked": True},
                content_type="application/json")

    assert r.status_code == 403
    contact.refresh_from_db()
    assert not contact.is_blocked


def test_the_identity_columns_still_cannot_be_edited():
    """Editing what a record is keyed on would silently re-attribute a
    history to someone else."""
    ws, app = _ws(), _app()
    contact = services.record_embed_visitor(workspace=ws, app=app, external_id="u-42")
    c = _client(ws)

    r = c.patch(f"/api/contacts/{contact.pk}/",
                data={"external_id": "u-99", "source": "email"},
                content_type="application/json")

    assert r.status_code == 422
    contact.refresh_from_db()
    assert contact.external_id == "u-42"


def _logged(monkeypatch) -> list[str]:
    """Every message the services module logs, independent of Django's logging
    config — `caplog` sees nothing here because the app logger does not
    propagate to root, and a test that silently captures nothing would pass no
    matter what the code did.
    """
    from apps.contacts import services as svc

    lines: list[str] = []

    class Spy:
        def info(self, msg, *args):
            lines.append(msg % args if args else msg)

        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(svc, "logger", Spy())
    return lines


def test_a_persons_own_identifier_does_not_reach_the_application_log(monkeypatch):
    """CodeQL flagged this, and it was right for a reason worth keeping.

    `external_id` is chosen by the host and canopy cannot know what it is — an
    opaque uuid for one site, an email or a phone number for the next. The
    database row holds it under an ACL; an application log is read by more
    people, retained by different rules, and shipped somewhere else. The pk
    correlates just as well.
    """
    lines = _logged(monkeypatch)
    ws, app = _ws(), _app()

    services.record_embed_visitor(
        workspace=ws, app=app, external_id="alice@partner.example",
        email="alice@partner.example",
    )

    logged = " ".join(lines)
    assert logged, "nothing was logged, so this test would pass for the wrong reason"
    assert "alice@partner.example" not in logged
    assert str(Contact.objects.get().pk) in logged


def test_nor_does_an_email_contacts_address(monkeypatch):
    lines = _logged(monkeypatch)
    ws = _ws()

    services.record_inbound_sender(workspace=ws, address="p@llo.org")

    assert lines and "p@llo.org" not in " ".join(lines)
