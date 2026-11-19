"""Copy today's agent-by-agent review grants into the program-level shape.

Every live `SessionReviewGrant` becomes: one `CoachingOptIn` for its person, and a `CoachingRole`
for the agent they named in that agent's own workspace (so exactly the people who could read them
before still can). The agents the product names as coaches today (`COACHING_AGENT_SLUGS`) also get
a role in their own workspace, so a person who opts in from now on is coached by them. Idempotent."""
from django.conf import settings
from django.db import migrations


def copy(apps, schema_editor):
    Grant = apps.get_model("canopy_sessions", "SessionReviewGrant")
    OptIn = apps.get_model("canopy_sessions", "CoachingOptIn")
    Role = apps.get_model("canopy_sessions", "CoachingRole")
    Agent = apps.get_model("agents", "Agent")

    def ensure_role(agent):
        if agent is not None and agent.workspace_id:
            Role.objects.get_or_create(workspace_id=agent.workspace_id, agent=agent, revoked_at=None)

    for g in Grant.objects.filter(revoked_at__isnull=True).select_related("agent").order_by("created_at"):
        if not OptIn.objects.filter(user_id=g.user_id, revoked_at__isnull=True).exists():
            row = OptIn.objects.create(user_id=g.user_id)
            OptIn.objects.filter(pk=row.pk).update(created_at=g.created_at)
        ensure_role(g.agent)
    for slug in [s.strip() for s in str(getattr(settings, "COACHING_AGENT_SLUGS", "")).split(",") if s.strip()]:
        ensure_role(Agent.objects.filter(slug=slug).first())


class Migration(migrations.Migration):
    dependencies = [("canopy_sessions", "0071_coaching_opt_in_and_roles"), ("agents", "__latest__")]
    operations = [migrations.RunPython(copy, migrations.RunPython.noop)]
