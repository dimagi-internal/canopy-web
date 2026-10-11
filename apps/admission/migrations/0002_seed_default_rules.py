"""Seed the default admission rules into every workspace that exists now.

Workspaces created later get them from `apps/admission/signals.py`. The
defaults themselves are `apps/admission/defaults.py`: the email + confined +
manual + cloud reject (on), and its laptop twin (off). canopy-web#1400.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from apps.admission import defaults

    Workspace = apps.get_model("workspaces", "Workspace")
    AdmissionRule = apps.get_model("admission", "AdmissionRule")
    for slug in Workspace.objects.values_list("slug", flat=True):
        defaults.seed(slug, rule_model=AdmissionRule)


def unseed(apps, schema_editor):
    AdmissionRule = apps.get_model("admission", "AdmissionRule")
    from apps.admission import defaults

    AdmissionRule.objects.filter(seed_key__in=[d["seed_key"] for d in defaults.DEFAULTS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("admission", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
