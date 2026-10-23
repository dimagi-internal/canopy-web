from django.db import migrations, models


def carry_over(apps, schema_editor):
    """`directed` keeps what each box was: a person's override where one was set,
    else what the box declared (`capabilities.directed`, now no longer read)."""
    Runner = apps.get_model("harness", "Runner")
    for r in Runner.objects.all().only("pk", "capabilities", "directed_setting"):
        caps = r.capabilities or {}
        directed = (r.directed_setting if r.directed_setting is not None
                    else bool(caps.get("directed", False)))
        caps.pop("directed", None)
        Runner.objects.filter(pk=r.pk).update(directed=directed, capabilities=caps)


class Migration(migrations.Migration):

    dependencies = [
        ("harness", "0106_runner_slash_commands"),
    ]

    operations = [
        migrations.AddField(
            model_name="runner",
            name="directed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="runnerinvitation",
            name="directed",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(carry_over, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="runner",
            name="directed_setting",
        ),
    ]
