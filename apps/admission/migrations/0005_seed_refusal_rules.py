"""Seed the two refusal rules (`blocked-sender`, `offered-nothing`) into every
workspace that exists now; new workspaces get them from `signals.py`.
canopy-web#1416. Only these two keys: a 0002 default a workspace has since
deleted stays deleted."""
from django.db import migrations

KEYS = ("blocked-sender", "offered-nothing")


def seed(apps, schema_editor):
    from apps.admission import defaults

    Workspace = apps.get_model("workspaces", "Workspace")
    AdmissionRule = apps.get_model("admission", "AdmissionRule")
    for slug in Workspace.objects.values_list("slug", flat=True):
        defaults.seed(slug, rule_model=AdmissionRule, keys=KEYS)


def unseed(apps, schema_editor):
    apps.get_model("admission", "AdmissionRule").objects.filter(seed_key__in=KEYS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("admission", "0004_refusal_conditions"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
