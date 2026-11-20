"""`brain.eligible`, row by row. Asked on every request, caches nothing, fails closed."""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.brain import consent
from apps.brain.eligibility import eligible, rule_denies
from apps.brain.models import BrainSource, SourceExclusion
from apps.contacts.models import Contact
from apps.harness import runner_requirements as rr
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.brain_world import agree, brain_world, capability, member, user

pytestmark = [pytest.mark.django_db, pytest.mark.guard]


@pytest.fixture()
def w():
    return brain_world()


def ok(w, cap, purpose, subject=None):
    return eligible(w["source"], w[cap], purpose, subject)


# ---- about a person: only the subject's consent counts -------------------------------------------

def test_nothing_is_eligible_before_anyone_agrees(w):
    assert not ok(w, "coach", "coach", w["alice"])
    assert ok(w, "coach", "coach", w["alice"]).reason == f"no_consent:{w['alice'].pk}"


def test_individual_needs_the_subject_s_own_consent_and_a_subject(w):
    agree(w["alice"], "individual")
    assert ok(w, "coach", "coach", w["alice"])
    assert ok(w, "coach", "coach", w["alice"].pk)             # an id is as good as a user
    assert not ok(w, "coach", "coach", w["bob"])              # bob never agreed
    assert not ok(w, "coach", "coach", None)                  # about a person with no person
    assert not ok(w, "coach", "coach", w["carol"])            # carol is not in the source at all
    assert ok(w, "coach", "coach", w["carol"]).reason == "subject_not_a_participant"


def test_one_persons_consent_never_stands_for_the_other_s(w):
    agree(w["alice"], "individual")
    agree(w["bob"], "memory")
    assert not ok(w, "coach", "coach", w["bob"])              # bob agreed to memory, not individual
    assert ok(w, "mem", "remember", w["bob"])
    assert not ok(w, "mem", "remember", w["alice"])


def test_a_class_the_person_did_not_list_is_a_no(w):
    agree(w["alice"], "individual")
    assert not ok(w, "mem", "remember", w["alice"])


# ---- the capability ------------------------------------------------------------------------------

def test_the_capability_must_exist_be_enabled_and_declare_the_purpose(w):
    agree(w["alice"], "individual")
    assert not ok(w, "coach", "something-else", w["alice"])
    assert ok(w, "coach", "something-else", w["alice"]).reason == "purpose_not_declared"
    assert not eligible(w["source"], "no-such-capability", "coach", w["alice"])
    off = capability("off", "individual", ("coach",), enabled=False)
    assert not eligible(w["source"], off, "coach", w["alice"])
    assert eligible(w["source"], "coach", "coach", w["alice"])  # a slug is as good as a row


def test_an_in_hand_capability_is_never_trusted_over_the_stored_one(w):
    agree(w["alice"], "individual")
    stale = w["coach"]
    type(stale).objects.filter(pk=stale.pk).update(enabled=False)
    assert stale.enabled is True and not eligible(w["source"], stale, "coach", w["alice"])


# ---- shared uses: every human participant ---------------------------------------------------------

def test_a_shared_use_needs_every_human_participant(w):
    agree(w["alice"], "project")
    assert not ok(w, "proj", "learn")
    assert ok(w, "proj", "learn").reason == f"no_consent:{w['bob'].pk}"
    agree(w["bob"], "project", "individual")
    assert ok(w, "proj", "learn")
    assert not ok(w, "sys", "learn")                          # system is another class


def test_any_participant_leaving_a_shared_use_ends_it_but_not_the_other_s_own_use(w):
    agree(w["alice"], "project", "individual")
    agree(w["bob"], "project", "individual")
    consent.exclude(w["bob"], w["source"])
    assert not ok(w, "proj", "learn")                         # shared: any exclusion is a no
    assert ok(w, "proj", "learn").reason == f"excluded:{w['bob'].pk}"
    assert ok(w, "coach", "coach", w["alice"])                # what is learned about alice from her words
    assert not ok(w, "coach", "coach", w["bob"])              # bob's own exclusion shuts bob's
    consent.include(w["bob"], w["source"])
    assert ok(w, "proj", "learn")


def test_a_contact_in_the_source_makes_it_ineligible_for_every_shared_use(w):
    agree(w["alice"], "project", "individual")
    agree(w["bob"], "project")
    from apps.brain.models import SourceParticipant

    c = Contact.objects.create(workspace=w["ws"], email="x@example.org")
    SourceParticipant.objects.create(source=w["source"], contact=c)
    assert ok(w, "proj", "learn").reason == "contact_in_source"
    assert ok(w, "coach", "coach", w["alice"])                # alice's own use is unaffected


def test_a_source_with_no_human_participant_is_a_no(w):
    agree(w["alice"], "project")
    type(w["session"]).objects.filter(pk=w["session"].pk).update(created_by=None)
    w["session"].participants.all().delete()
    w["source"].participants.all().delete()
    assert ok(w, "proj", "learn").reason == "no_human_participant"


# ---- sealed, withdrawn ---------------------------------------------------------------------------

@pytest.mark.parametrize("seal,why", [
    ({"metadata": {rr.METADATA_KEY: ["sensitive-data"]}}, "sensitive"),
    ({"metadata": {rr.METADATA_KEY: "anything-at-all"}}, "sensitive"),        # a new flag fails closed
    ({"content_deleted_at": timezone.now()}, "words_deleted"),
])
def test_a_sealed_session_is_closed_to_every_use_and_is_read_live(w, seal, why):
    agree(w["alice"], "individual")
    assert ok(w, "coach", "coach", w["alice"])
    type(w["session"]).objects.filter(pk=w["session"].pk).update(**seal)      # nothing refreshed the source
    assert ok(w, "coach", "coach", w["alice"]).reason == f"sealed:{why}"


def test_a_contacts_conversation_is_sealed(w):
    agree(w["alice"], "individual")
    c = Contact.objects.create(workspace=w["ws"], email="y@example.org")
    type(w["session"]).objects.filter(pk=w["session"].pk).update(contact=c)
    assert ok(w, "coach", "coach", w["alice"]).reason == "sealed:contact"


def test_a_withdrawn_source_is_a_no(w):
    agree(w["alice"], "individual")
    BrainSource.objects.filter(pk=w["source"].pk).update(state=BrainSource.WITHDRAWN)
    assert ok(w, "coach", "coach", w["alice"]).reason == "withdrawn"


def test_a_session_that_is_gone_is_a_no(w):
    agree(w["alice"], "individual")
    w["session"].delete()
    assert not ok(w, "coach", "coach", w["alice"])


# ---- revocation, workspaces, rules, membership ---------------------------------------------------

def test_revoking_changes_the_very_next_answer(w):
    agree(w["alice"], "individual")
    assert ok(w, "coach", "coach", w["alice"])
    consent.revoke(w["alice"])
    assert not ok(w, "coach", "coach", w["alice"])


def test_the_consent_s_workspaces_bound_it_and_include_new_decides_the_ones_after(w):
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=w["owner"])
    member(w["alice"], other)
    agree(w["alice"], "individual", workspaces=["other"], include_new=False)
    assert ok(w, "coach", "coach", w["alice"]).reason == f"outside_workspaces:{w['alice'].pk}"
    agree(w["alice"], "individual", workspaces=["other"], include_new=False)
    third = Workspace.objects.create(slug="third", display_name="Third", created_by=w["owner"])
    member(w["alice"], third)
    row = consent.live(w["alice"])
    from apps.brain.eligibility import covers

    assert covers(row, "other") and not covers(row, "third")                  # joined after, and not wanted
    row.include_new = True
    assert covers(row, "third") and not covers(row, "acme")                   # acme was known and not listed


def test_no_workspaces_means_every_one_of_the_person_s(w):
    agree(w["alice"], "individual")
    assert ok(w, "coach", "coach", w["alice"])


def test_a_standing_rule_denies_the_next_read(w):
    agree(w["alice"], "individual")
    assert ok(w, "coach", "coach", w["alice"])
    agree(w["alice"], "individual", standing_rules=[{"capability": "coach"}])
    assert ok(w, "coach", "coach", w["alice"]).reason == f"standing_rule:{w['alice'].pk}"
    agree(w["alice"], "individual", standing_rules=[{"capability": "coach", "purpose": "other"}])
    assert ok(w, "coach", "coach", w["alice"])                                # names a purpose this is not
    agree(w["alice"], "individual", standing_rules=[{"workspace": "acme"}])
    assert not ok(w, "coach", "coach", w["alice"])


def test_a_rule_that_cannot_be_read_denies():
    kw = dict(capability="c", purpose="p", cls="individual", workspace="w")
    assert rule_denies("junk", **kw) and rule_denies({}, **kw) and rule_denies({"surprise": "x"}, **kw)
    assert not rule_denies({"capability": "other"}, **kw)


def test_someone_who_left_the_workspace_is_no_longer_eligible(w):
    agree(w["alice"], "individual")
    WorkspaceMembership.objects.filter(user=w["alice"], workspace=w["ws"]).delete()
    assert ok(w, "coach", "coach", w["alice"]).reason == f"not_a_member:{w['alice'].pk}"


def test_a_guest_is_outside_the_workspace_for_the_brain_too(w):
    agree(w["alice"], "individual")
    WorkspaceMembership.objects.filter(user=w["alice"], workspace=w["ws"]).update(role="guest")
    assert not ok(w, "coach", "coach", w["alice"])


def test_an_agent_s_login_is_never_a_participant(w):
    from apps.agents.models import Agent
    from apps.brain import sources
    from apps.canopy_sessions.models import Session, SessionParticipant

    bot = Agent.objects.create(slug="hal", name="Hal", owner=w["owner"], workspace=w["ws"])
    bot.user = user("hal_login")
    bot.save(update_fields=["user"])
    s = Session.objects.create(workspace=w["ws"], created_by=w["alice"], origin=Session.ORIGIN_WEB, agent=bot)
    SessionParticipant.objects.create(session=s, user=bot.user)
    src = sources.for_session(s)
    assert set(src.participants.values_list("user_id", flat=True)) == {w["alice"].pk}


def test_an_exclusion_row_is_all_it_takes(w):
    agree(w["alice"], "individual")
    SourceExclusion.objects.create(user=w["alice"], source=w["source"])
    assert not ok(w, "coach", "coach", w["alice"])


# ---- `explain`: the same judgement, for a class and with every missing consent assumed --------------

def test_explain_is_eligible_and_for_a_class_is_everyone_agreed(w):
    from apps.brain.eligibility import everyone_agreed, explain

    agree(w["alice"], "project")
    assert explain(w["source"], w["proj"], "learn") == ok(w, "proj", "learn")
    assert explain(w["source"], cls="project").reason == f"no_consent:{w['bob'].pk}"
    assert not everyone_agreed(w["source"], "project")
    agree(w["bob"], "project")
    assert explain(w["source"], cls="project").ok and everyone_agreed(w["source"], "project")


def test_assuming_consent_keeps_every_other_fence(w):
    from apps.brain.eligibility import explain
    from apps.canopy_sessions.models import Session

    def assumed(**kw):
        return explain(w["source"], cls="project", assume_consent=True, **kw)

    assert assumed().ok                                              # nobody agreed: assumed
    consent.set_consent(w["bob"], classes=["system"])                # a class missing from a live consent: assumed
    assert assumed().ok
    consent.revoke(w["bob"])                                         # ended: their act, not a gap
    assert assumed().reason == f"no_consent:{w['bob'].pk}"
    consent.set_consent(w["bob"], classes=["project"], standing_rules=[{"class": "project"}])
    assert assumed().reason == f"standing_rule:{w['bob'].pk}"
    consent.set_consent(w["bob"], classes=["project"], standing_rules=[])
    WorkspaceMembership.objects.filter(user=w["bob"]).delete()
    assert assumed().reason == f"not_a_member:{w['bob'].pk}"
    member(w["bob"], w["ws"])
    SourceExclusion.objects.create(user=w["bob"], source=w["source"])
    assert assumed().reason == f"excluded:{w['bob'].pk}"
    # A session no source was made for is judged from the session itself.
    fresh = Session.objects.create(workspace=w["ws"], created_by=w["carol"], origin=Session.ORIGIN_WEB)
    assert explain(session=fresh, cls="project", assume_consent=True).ok
    assert not BrainSource.objects.filter(ref=str(fresh.pk)).exists()
    Session.objects.filter(pk=fresh.pk).update(metadata={rr.METADATA_KEY: ["phi"]})
    fresh.refresh_from_db()
    assert explain(session=fresh, cls="project", assume_consent=True).reason == "sealed:sensitive"
