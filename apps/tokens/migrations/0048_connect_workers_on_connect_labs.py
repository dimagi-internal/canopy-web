"""Connect Labs' Connect Worker messaging moves onto its one host site, `connect-labs`, as the
`workers` surface (docs/architecture/host-sites.md § Surfaces).

In workspace `connect`, for `connect-helper` and `connect-coach`, whose `contact_scoped` entry
named the separate `connect-messaging` site:

* the entry now names `connect-labs` and serves its `workers` surface. Its key stays, so the
  access lines that name it (`use:<key>`) and the turns stamped with it still hold;
* `connect-labs` may offer both agents (`AppCredentialAgent`);
* `connect-labs` pushes its relayed replies to labs' reply webhook, set here only when the site
  is Connect Labs' production site and has no webhook yet;
* the `connect-messaging` site, if it was ever registered there, is revoked: labs no longer
  signs as it.

A database without `connect-labs` in `connect` (every test and development database) is left
as it is. Running it twice changes nothing.
"""

from django.db import migrations
from django.utils import timezone

WORKSPACE = "connect"
OLD_SITE = "connect-messaging"
SITE = "connect-labs"
SURFACE = "workers"
AGENTS = ("connect-helper", "connect-coach")
LABS = "https://labs.connect.dimagi.com"
REPLY_WEBHOOK = f"{LABS}/messaging/canopy/replies/"


def _repoint(caps: dict) -> bool:
    """Point the entry naming `OLD_SITE` at `SITE`'s `workers` surface. False when the
    agent has no such entry, or already has an entry for `SITE` (one entry per site)."""
    if any(SITE in ((cap or {}).get("sites") or []) for cap in caps.values()):
        return False
    for cap in caps.values():
        sites = (cap or {}).get("sites") or []
        if OLD_SITE not in sites:
            continue
        cap["sites"] = [SITE if s == OLD_SITE else s for s in sites]
        cap["surface"] = SURFACE
        own = cap.get("own_login") or {}
        if OLD_SITE in own:
            own[SITE] = own.pop(OLD_SITE)
        return True
    return False


def forward(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    AppCredential = apps.get_model("tokens", "AppCredential")
    AppCredentialAgent = apps.get_model("tokens", "AppCredentialAgent")

    site = AppCredential.objects.filter(name=SITE, workspace__slug=WORKSPACE, revoked_at__isnull=True).first()
    if site is None:
        return
    for slug in AGENTS:
        agent = Agent.objects.filter(slug=slug, workspace_id=site.workspace_id).first()
        if agent is None:
            continue
        doc = dict(agent.interface or {})
        caps = dict(doc.get("capabilities") or {})
        if _repoint(caps):
            doc["capabilities"] = caps
            agent.interface = doc
            agent.save(update_fields=["interface"])
        if any(SITE in ((cap or {}).get("sites") or []) for cap in caps.values()):
            AppCredentialAgent.objects.get_or_create(app=site, agent=agent)
    if not site.reply_webhook_url and (site.host_mcp_resource or "").startswith(f"{LABS}/"):
        site.reply_webhook_url = REPLY_WEBHOOK
        site.reply_mode = "final"
        site.save(update_fields=["reply_webhook_url", "reply_mode"])
    AppCredential.objects.filter(name=OLD_SITE, workspace__slug=WORKSPACE, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0143_creator_agent_admins"),
        ("tokens", "0047_reply_push"),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
