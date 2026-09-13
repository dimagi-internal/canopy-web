# Renaming auto_join_domains -> self_join_domains. RenameField (not a
# drop-and-recreate via RemoveField+AddField) so the existing rows' values
# (dimagi.com / dimagi-associate.com on the default workspace) survive the
# rename rather than being reset to []. Verified against the live labs DB
# before this shipped: both domains present on `dimagi` after migrating.
#
# THE SIX EXTRA DEPENDENCIES ARE LOAD-BEARING, not tidiness. Six already-shipped
# migrations write the OLD field name at runtime — five backfills pass
# `"auto_join_domains": domains` to a get_or_create on the historical Workspace
# model, and agents/0013 passes it as a kwarg. An immutable migration cannot be
# edited to use the new name, so every one of them raises TypeError/FieldError
# if it runs AFTER this rename.
#
# Today they happen to run before it, but only because Django's graph sorts
# unrelated leaves by app label and `walkthroughs` < `workspaces` — an ordering
# nothing declares and nothing tests. A new app whose label sorts after
# `workspaces`, depending on `workspaces` (which any tenant-carrying app does),
# could reorder this rename ahead of them and break `migrate` on a fresh
# database. Naming them as dependencies makes "rename last" a fact of the graph.
#
# No cycle: each of the six depends on `workspaces/0002` at the latest, and
# nothing depends on this migration yet.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workspaces', '0007_workspace_shared_op_sa_token_enc_and_more'),
        # Must run AFTER every migration that writes `auto_join_domains`.
        ('agents', '0007_backfill_default_workspace'),
        ('agents', '0013_agent_workspace_not_null'),
        ('projects', '0007_backfill_default_workspace'),
        ('reviews', '0007_backfill_default_workspace'),
        ('shareouts', '0005_backfill_default_workspace'),
        ('walkthroughs', '0007_backfill_default_workspace'),
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
