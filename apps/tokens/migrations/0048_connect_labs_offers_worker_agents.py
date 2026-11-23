"""Connect Labs' one host site, `connect-labs`, carries its Connect Worker messaging as the
`workers` surface (docs/architecture/host-sites.md § Surfaces).

In workspace `connect`:

* `connect-labs` may offer `connect-helper` and `connect-coach` (`AppCredentialAgent`), the
  site's half of serving them on its `workers` surface. Each agent's own entry for the site is
  set by its admin (`PUT /api/agents/{slug}/host-sites/connect-labs`, `surface: workers`);
* the separate `connect-messaging` site, if it is registered there, is revoked: labs signs
  only as `connect-labs`.

The site's reply webhook is not set here: it receives the relayed conversations' words, so it
is a workspace owner's change behind a passkey step-up (`update_host_site`).

A database without `connect-labs` in `connect` (every test and development database) is left
as it is. Running it twice changes nothing.
"""

from django.db import migrations
from django.utils import timezone

WORKSPACE = "connect"
OLD_SITE = "connect-messaging"
SITE = "connect-labs"
AGENTS = ("connect-helper", "connect-coach")


def forward(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    AppCredential = apps.get_model("tokens", "AppCredential")
    AppCredentialAgent = apps.get_model("tokens", "AppCredentialAgent")

    site = AppCredential.objects.filter(name=SITE, workspace__slug=WORKSPACE, revoked_at__isnull=True).first()
    if site is None:
        return
    for agent in Agent.objects.filter(slug__in=AGENTS, workspace_id=site.workspace_id):
        AppCredentialAgent.objects.get_or_create(app=site, agent=agent)
    AppCredential.objects.filter(name=OLD_SITE, workspace__slug=WORKSPACE, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0143_creator_agent_admins"),
        ("tokens", "0047_reply_push"),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
