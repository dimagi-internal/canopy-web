"""An agent's canopy login is the account at its own email. Link the ones that were
never linked, where that is unambiguous: an active account whose email equals the
agent's, that is no other agent's login and has never signed in interactively (so it is
nobody's personal account). Anything else waits for the in-app flow, where the person
holding the account accepts."""
from django.db import migrations


def link(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    User = apps.get_model("auth", "User")
    taken = set(Agent.objects.exclude(user=None).values_list("user_id", flat=True))
    for agent in Agent.objects.filter(user=None).exclude(email=""):
        address = agent.email.strip()
        if "@" not in address:
            continue
        found = list(User.objects.filter(email__iexact=address, is_active=True)[:2])
        if len(found) != 1:
            continue
        account = found[0]
        if account.pk in taken or account.last_login is not None:
            continue
        agent.user_id = account.pk
        agent.save(update_fields=["user"])
        taken.add(account.pk)


class Migration(migrations.Migration):
    dependencies = [("agents", "0095_agent_login_request")]
    operations = [migrations.RunPython(link, migrations.RunPython.noop)]
