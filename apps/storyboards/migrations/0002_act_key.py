"""Give every act a stable key.

The backfill sits BETWEEN the add and the constraint on purpose: every existing
act would otherwise share the default `""` and the unique constraint would
refuse to build on any board with more than one act.

Keys are derived exactly as the write paths derive them, so re-importing a
storyboard file after this migration reproduces the same keys and leaves
existing act notes attached.
"""
from django.db import migrations, models


def backfill(apps, schema_editor):
    from apps.storyboards.act_keys import act_key

    Act = apps.get_model("storyboards", "Act")
    Storyboard = apps.get_model("storyboards", "Storyboard")
    for board in Storyboard.objects.all():
        taken = set()
        for pos, act in enumerate(board.acts.order_by("position", "id")):
            act.key = act_key("", act.title, pos, taken)
            act.save(update_fields=["key"])


def noop(apps, schema_editor):
    """Reversing drops the column anyway."""


class Migration(migrations.Migration):

    dependencies = [
        ('storyboards', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='act',
            name='key',
            field=models.SlugField(blank=True, default='', max_length=120),
        ),
        migrations.RunPython(backfill, noop),
        migrations.AddConstraint(
            model_name='act',
            constraint=models.UniqueConstraint(fields=('storyboard', 'key'), name='uniq_act_storyboard_key'),
        ),
    ]
