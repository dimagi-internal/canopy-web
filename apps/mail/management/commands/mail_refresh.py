# apps/mail/management/commands/mail_refresh.py
from django.core.management.base import BaseCommand

from apps.mail import domains
from apps.mail.models import MailDomain


class Command(BaseCommand):
    help = "Re-check every mail domain's SES verification and MX now."

    def handle(self, *args, **opts):
        for d in MailDomain.objects.all():
            d = domains.refresh(d, force=True)
            self.stdout.write(f"{d.name}: {d.status} receiving={d.receiving} mx={d.mx_provider}")
