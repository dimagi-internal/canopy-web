# Renaming auto_join_domains -> self_join_domains. RenameField (not a
# drop-and-recreate via RemoveField+AddField) so the existing rows' values
# (dimagi.com / dimagi-associate.com on the default workspace) survive the
# migration — see SELFJOIN-REPORT.md for the verified proof.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0007_workspace_shared_op_sa_token_enc_and_more'),
    ]

    operations = [
        migrations.RenameField(
            model_name='workspace',
            old_name='auto_join_domains',
            new_name='self_join_domains',
        ),
        migrations.AlterField(
            model_name='workspace',
            name='self_join_domains',
            field=models.JSONField(
                default=list,
                blank=True,
                help_text=(
                    "Email domains (lowercased, no leading '@') whose users may "
                    "JOIN this workspace themselves, by explicit action. Not "
                    "automatic — see POST /api/workspaces/{slug}/join."
                ),
            ),
        ),
    ]
