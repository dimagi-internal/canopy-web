"""Carry every live `CoachingOptIn` over as an `individual` `LearningConsent`.

Additive: the opt-in table stays (coaching's session ACL still reads it) and a later release drops it.
Idempotent, and it touches no row another table references, so it needs no migration of its own."""
from django.db import migrations


def copy(apps, schema_editor):
    CoachingOptIn = apps.get_model("canopy_sessions", "CoachingOptIn")
    LearningConsent = apps.get_model("brain", "LearningConsent")
    have = set(LearningConsent.objects.filter(revoked_at__isnull=True).values_list("user_id", flat=True))
    for opt in CoachingOptIn.objects.filter(revoked_at__isnull=True).order_by("created_at", "pk"):
        if opt.user_id in have:
            continue
        row = LearningConsent.objects.create(user_id=opt.user_id, classes=["individual"])
        LearningConsent.objects.filter(pk=row.pk).update(created_at=opt.created_at)  # auto_now_add
        have.add(opt.user_id)


class Migration(migrations.Migration):
    dependencies = [("brain", "0001_initial"), ("canopy_sessions", "0072_copy_review_grants_to_coaching")]
    operations = [migrations.RunPython(copy, migrations.RunPython.noop)]
