"""Work groups are gone: their rows became check-ins in 0004."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("huddles", "0004_work_groups_become_checkins"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="huddlegroup",
            name="uniq_huddle_group_slug",
        ),
        migrations.RemoveIndex(
            model_name="huddlegrouprun",
            name="huddles_hud_status_9266dc_idx",
        ),
        migrations.RemoveConstraint(
            model_name="huddlegrouprun",
            name="uniq_huddle_group_run_slot",
        ),
        migrations.RemoveField(
            model_name="huddlegroup",
            name="created_by",
        ),
        migrations.RemoveField(
            model_name="huddlegroup",
            name="installation",
        ),
        migrations.RemoveField(
            model_name="huddlegroup",
            name="lead",
        ),
        migrations.RemoveField(
            model_name="huddlegroup",
            name="leader_agent",
        ),
        migrations.RemoveField(
            model_name="huddlegroup",
            name="project",
        ),
        migrations.RemoveField(
            model_name="huddlegroup",
            name="schedule",
        ),
        migrations.RemoveField(
            model_name="huddlegroup",
            name="workspace",
        ),
        migrations.RemoveField(
            model_name="huddlegrouprun",
            name="confirmed_by",
        ),
        migrations.RemoveField(
            model_name="huddlegrouprun",
            name="group",
        ),
        migrations.RemoveField(
            model_name="huddlegrouprun",
            name="ingest",
        ),
        migrations.RemoveField(
            model_name="huddlegrouprun",
            name="turn",
        ),
        migrations.DeleteModel(
            name="HuddleGroup",
        ),
        migrations.DeleteModel(
            name="HuddleGroupRun",
        ),
    ]
