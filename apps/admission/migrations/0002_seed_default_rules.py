"""Seed the default admission rules into every workspace that exists now.

Workspaces created later get them from `apps/admission/signals.py`. The
defaults themselves are `apps/admission/defaults.py`: the email + confined +
manual + cloud reject (on), and its laptop twin (off). canopy-web#1400.
"""
from django.db import migrations

#: The defaults this migration seeds. Pinned: later defaults carry fields this
#: migration's historical model does not have (they are 0005's).
KEYS = ("email-confined-manual-cloud", "email-confined-manual-laptop")


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
        ("admission", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
