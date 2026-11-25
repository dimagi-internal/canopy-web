"""The one "who is this about" question (`BrainRun.subject_user_id`, `BrainOutput.subject_user_id`): a person's user id,
or None when the subject is not a person or not a user id. A run whose subject is not a user id reads nothing and
emits nothing; it never raises."""
from __future__ import annotations

import pytest

from apps.brain import emit, reading
from apps.brain.models import BrainOutput, BrainRun, person_id, project_id
from tests.fixtures.brain_world import agree, brain_world

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(("subject_type", "subject", "person", "project"), [
    ("person", "12", 12, None), ("person", "alice", None, None), ("person", "", None, None),
    ("project", "7", None, 7), ("system", "acme", None, None),
])
def test_one_rule_says_who_or_what_a_subject_is(subject_type, subject, person, project):
    assert person_id(subject_type, subject) == person
    assert project_id(subject_type, subject) == project
    assert BrainRun(subject_type=subject_type, subject=subject).subject_user_id == person
    out = BrainOutput(subject_type=subject_type, subject=subject)
    assert (out.subject_user_id, out.subject_project_id) == (person, project)


def test_a_run_about_no_user_id_reads_nothing_and_emits_nothing():
    w = brain_world()
    agree(w["alice"], "individual")
    w["coach"].output_kinds = ["note"]
    w["coach"].save()
    good = BrainRun.objects.create(capability=w["coach"], subject_type="person", subject=str(w["alice"].pk),
                                   purpose="coach")
    assert list(reading.candidates(good)) == [w["source"]]
    good.delete()
    bad = BrainRun.objects.create(capability=w["coach"], subject_type="person", subject="alice", purpose="coach")
    assert list(reading.candidates(bad)) == []                      # no ValueError: a person no one is
    with pytest.raises(emit.EmitRefusedError):
        emit.emit(bad, kind="note", title="t", body={}, source_ids=[w["source"].pk])
