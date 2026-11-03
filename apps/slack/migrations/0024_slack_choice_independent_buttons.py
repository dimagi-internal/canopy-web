from django.db import migrations, models


def copy_choices(apps, schema_editor):
    """A post already answered keeps its answer in the new shape."""
    Post = apps.get_model("slack", "SlackChoicePost")
    for post in Post.objects.filter(chosen_at__isnull=False):
        post.chosen = {str(post.chosen_index): {"by": post.chosen_by, "at": post.chosen_at.isoformat()}}
        post.save(update_fields=["chosen"])


class Migration(migrations.Migration):
    dependencies = [
        ("slack", "0023_slack_choice_post"),
    ]

    operations = [
        # Every post made so far was made as one choice, so stays one; a new one need not be.
        migrations.AddField(
            model_name="slackchoicepost",
            name="exclusive",
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name="slackchoicepost",
            name="exclusive",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="slackchoicepost",
            name="chosen",
            field=models.JSONField(default=dict),
        ),
        migrations.RunPython(copy_choices, migrations.RunPython.noop),
    ]
