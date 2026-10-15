from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_route_workspace(apps, schema_editor):
    MailRoute = apps.get_model("mail", "MailRoute")
    for r in MailRoute.objects.select_related("domain").filter(workspace__isnull=True):
        MailRoute.objects.filter(pk=r.pk).update(workspace_id=r.domain.workspace_id)


class Migration(migrations.Migration):
    dependencies = [
        ("mail", "0003_message_reply_to"),
        ("workspaces", "0013_system_accounts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="mailroute",
            name="workspace",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name="mail_routes", to="workspaces.workspace"),
        ),
        migrations.RunPython(backfill_route_workspace, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="mailroute",
            name="workspace",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                    related_name="mail_routes", to="workspaces.workspace"),
        ),
        migrations.CreateModel(
            name="MailDomainShare",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("daily_send_cap", models.PositiveIntegerField(default=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
                ("domain", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shares",
                                             to="mail.maildomain")),
                ("workspace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                related_name="mail_domain_shares", to="workspaces.workspace")),
            ],
            options={"ordering": ["domain__name", "workspace_id"]},
        ),
        migrations.AddConstraint(
            model_name="maildomainshare",
            constraint=models.UniqueConstraint(fields=("domain", "workspace"), name="mail_domain_share_once"),
        ),
    ]
