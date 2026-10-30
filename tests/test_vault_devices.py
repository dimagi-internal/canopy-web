"""`vault_members.devices_working_for`: which machines work for a set of people."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.agents.credentials import vault_members
from apps.harness.models import Runner, RunnerDeviceKey
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def test_a_machine_works_for_its_runners_owner_else_whoever_approved_its_key():
    User = get_user_model()
    mine, theirs, approver = (User.objects.create_user(username=n, email=f"{n}@dimagi.com") for n in ("m", "t", "a"))
    ws = a_workspace()
    owned = Runner.objects.create(name="mine", kind=Runner.CLOUD, owner=mine, workspace=ws)
    other = Runner.objects.create(name="theirs", kind=Runner.CLOUD, owner=theirs, workspace=ws)
    ownerless = Runner.objects.create(name="shared", kind=Runner.CLOUD, owner=None, workspace=ws)
    keys = {r.name: RunnerDeviceKey.objects.create(runner=r, jkt=f"j-{r.name}", kind=RunnerDeviceKey.SOFTWARE,
                                                   status=RunnerDeviceKey.ACTIVE, approved_by=approver)
            for r in (owned, other, ownerless)}
    for k in keys.values():
        assert vault_members.person_of_device(k) == (k.runner.owner_id or approver.pk)

    def working_for(*ids):
        return {k.runner.name for k in vault_members.devices_working_for(RunnerDeviceKey.objects.all(), list(ids))}

    assert working_for(mine.pk) == {"mine"}
    assert working_for(theirs.pk, mine.pk) == {"mine", "theirs"}
    assert working_for(approver.pk) == {"shared"}   # an ownerless machine works for its approver
    assert working_for() == set()
