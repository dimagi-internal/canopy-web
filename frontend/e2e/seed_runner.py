"""E2E seed for the fake runner (`e2e/fake_runner.py`): a cloud runner the e2e user
owns, enrolled in the workspace and able to run Echo's chats, plus a token for it.
Writes the runner's id and token to .auth/ for the runner process. Run after
seed.py via `manage.py shell -c`."""
import os

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment, RunnerEnrollment
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace

user = get_user_model().objects.get(username="e2e")
ws = Workspace.objects.get(slug="dimagi")
echo = Agent.objects.get(slug="echo")
if echo.owner_id != user.pk:
    echo.owner = user
    echo.save(update_fields=["owner"])

runner, _ = Runner.objects.update_or_create(
    name="e2e-fake-runner",
    defaults=dict(
        kind=Runner.CLOUD, host="e2e-fake-runner", owner=user, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), ready=True, ready_note="",
        capabilities={"sessions": True},
    ),
)
RunnerEnrollment.objects.get_or_create(runner=runner, workspace=ws, defaults={"enrolled_by": user})
RunnerAssignment.objects.get_or_create(agent=echo, runner=runner, source="", actor="",
                                       defaults={"rank": 0})
raw, _ = PersonalToken.create_for_user(user=user, label="e2e fake runner", ttl_days=0)

auth = os.path.join("frontend", "e2e", ".auth")
os.makedirs(auth, exist_ok=True)
with open(os.path.join(auth, "runner-id.txt"), "w") as f:
    f.write(str(runner.id))
with open(os.path.join(auth, "runner-token.txt"), "w") as f:
    f.write(raw)
print(f"e2e fake runner {runner.id}")
