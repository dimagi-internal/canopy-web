"""``manage.py canopy_artifacts_sweep`` — delete expired host-held results
(``canopy_sdk.django.artifacts.sweep``). Run it on a schedule (hourly is plenty):
an expired artifact already reads as ``expired``; this only frees the rows."""
from django.core.management.base import BaseCommand

from ...artifacts import sweep


class Command(BaseCommand):
    help = "Delete expired canopy host artifacts (results a tool produced)."

    def handle(self, *args, **options):
        deleted = sweep()
        self.stdout.write(f"deleted {deleted} expired artifact(s)")
