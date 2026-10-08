"""Mirror every contact's free-text `notes` into a `role` fact (fleet brain v1.1).

    manage.py mirror_contact_notes

Idempotent: a contact whose live mirrored fact already says what its notes say
is left alone; changed notes supersede it. Migration `contacts/0011` did this
once on deploy; `PATCH /api/contacts/{id}/` keeps it current. This command is
for re-running it by hand.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Mirror Contact.notes into PersonFact rows (idempotent)."

    def handle(self, *args, **opts):
        from apps.contacts import people

        result = people.mirror_all_contact_notes()
        self.stdout.write(f"contacts with notes: {result['contacts_with_notes']}, "
                          f"mirrored: {result['mirrored']}")
