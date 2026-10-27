"""A refresh remembers who asked, what the box was on, and when it finished, so the
runner page can say what changed and tell the person who asked."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("harness", "0125_system_accounts_to_triggers"), ("auth", "0012_alter_user_first_name_max_length")]
    operations = [
        migrations.AddField(
            model_name="runner", name="refresh_requested_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="+", to="auth.user"),
        ),
        migrations.AddField(
            model_name="runner", name="refresh_before",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="runner", name="refresh_finished_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
