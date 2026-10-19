# apps/mail/management/commands/mail_refresh.py
from django.core.management.base import BaseCommand

from apps.mail import domains
from apps.mail.models import MailDomain


class Command(BaseCommand):
    help = "Re-check every mail domain's SES verification and MX now."

    def handle(self, *args, **opts):
        for d in MailDomain.objects.all():
            try:
                d = domains.refresh(d, force=True)
            except Exception as exc:  # one bad domain must not stop the rest
                self.stderr.write(f"{d.name}: failed: {exc!r}")
                continue
            self.stdout.write(f"{d.name}: {d.status} receiving={d.receiving} mx={d.mx_provider}"
                              + (f" error={d.verification_error}" if d.verification_error else ""))
