"""An answered post keeps its answer (slack 0024). Delete once that migration has deployed."""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.canopy_sessions.models import Session

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("slack", "0023_slack_choice_post")]
AFTER = [("slack", "0025_remove_slack_choice_single_choice")]


def _posts_at(targets):
    """The post model as of `targets`, with the database migrated there."""
    MigrationExecutor(connection).migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps.get_model("slack", "SlackChoicePost")


@pytest.fixture(autouse=True)
def back_to_the_top():
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def test_posts_made_as_one_choice_stay_one_and_keep_their_answer(default_workspace):
    session = Session.objects.create(workspace=default_workspace, title="t")
    before = _posts_at(BEFORE)
    actions = [{"label": "A", "reply": "a"}, {"label": "B", "reply": "b"}]
    when = timezone.now()
    answered = before.objects.create(session_id=session.pk, channel_id="C", slack_ts="1", thread_key="k",
                                   actions=actions, chosen_at=when, chosen_index=1, chosen_by="U1")
    unanswered = before.objects.create(session_id=session.pk, channel_id="C", slack_ts="2", thread_key="k",
                                     actions=actions)

    after = _posts_at(AFTER)
    answered = after.objects.get(pk=answered.pk)
    assert answered.exclusive is True
    assert answered.chosen == {"1": {"by": "U1", "at": when.isoformat()}}
    unanswered = after.objects.get(pk=unanswered.pk)
    assert unanswered.exclusive is True and unanswered.chosen == {}
