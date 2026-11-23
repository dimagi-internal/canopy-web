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

Plain SQL rather than `RunPython`: a walk-back test unapplies and reapplies every migration
after its target, and a `RunPython` renders the whole project state each way. A database
without `connect-labs` in `connect` (every test and development database) is left as it is.
Running it twice changes nothing.
"""

from django.db import migrations

ALLOW_AGENTS = """
INSERT INTO app_credential_agents (app_id, agent_id, created_at, is_default)
SELECT site.id, agent.id, CURRENT_TIMESTAMP, FALSE
FROM app_credentials site
JOIN agents_agent agent ON agent.workspace_id = site.workspace_id
WHERE site.workspace_id = 'connect' AND site.name = 'connect-labs' AND site.revoked_at IS NULL
  AND agent.slug IN ('connect-helper', 'connect-coach')
  AND NOT EXISTS (SELECT 1 FROM app_credential_agents row
                  WHERE row.app_id = site.id AND row.agent_id = agent.id)
"""

REVOKE_CONNECT_MESSAGING = """
UPDATE app_credentials SET revoked_at = CURRENT_TIMESTAMP
WHERE workspace_id = 'connect' AND name = 'connect-messaging' AND revoked_at IS NULL
  AND EXISTS (SELECT 1 FROM app_credentials site
              WHERE site.workspace_id = 'connect' AND site.name = 'connect-labs' AND site.revoked_at IS NULL)
"""

SQL = (ALLOW_AGENTS, REVOKE_CONNECT_MESSAGING)


class Migration(migrations.Migration):
    # The tables it writes are long settled (agents_agent is far older than tokens 0047): no
    # dependency on a newer agents migration, which would pull this one into every walk-back of
    # agents' recent history.
    dependencies = [("tokens", "0047_reply_push")]

    operations = [migrations.RunSQL(sql, migrations.RunSQL.noop) for sql in SQL]
