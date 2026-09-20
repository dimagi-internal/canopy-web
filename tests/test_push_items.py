"""push must follow the badge. The waiting set counts open ASKS on tasks, so a new ask has to
mark its agent dirty — otherwise the phone and the badge silently disagree, which
is worse than no push at all."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone
from django.contrib.auth import get_user_model

from apps.agents.models import Agent
from apps.agents.models import AgentTask
from apps.harness.models import Turn
from apps.workspaces import services as wsvc


_EXT = iter(range(1, 10_000))


def _ext() -> str:
    """A unique `ext_id` per task these tests create. Tasks are board cards and
    carry one; an ask raised through the service gets it for free."""
    return f"T{next(_EXT)}"


pytestmark = pytest.mark.django_db


@pytest.fixture
def ada(db):
    get_user_model().objects.create_user(username="jj@dimagi.com", email="jj@dimagi.com")
    ws = wsvc.ensure_default_workspace()
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


def test_raising_an_item_marks_its_agent_dirty(ada):
    with patch("apps.push.signals.mark_dirty") as mark_dirty:
        AgentTask.objects.create(agent=ada, ext_id=_ext(), ask_kind=AgentTask.ASK_REVIEW, title="hal: discard 81 junk emails",
            origin=Turn.ORIGIN_API, idempotency_key="k1",
        )

    mark_dirty.assert_called_once_with(ada.id)


def test_deciding_an_item_marks_its_agent_dirty(ada):
    """A decided item leaves the waiting set. The count only drops, and push never
    sends on a drop — but the snapshot must still be updated, or the NEXT rise
    computes against a stale baseline and never fires."""
    item = AgentTask.objects.create(agent=ada, ext_id=_ext(), ask_kind=AgentTask.ASK_REVIEW, title="x", origin=Turn.ORIGIN_API,
        idempotency_key="k2",
    )

    with patch("apps.push.signals.mark_dirty") as mark_dirty:
        # Decided = answered; on a task that is `decided_at`, not a stored word.
        item.decided_at = timezone.now()
        item.save(update_fields=["decided_at"])

    mark_dirty.assert_called_once_with(ada.id)
