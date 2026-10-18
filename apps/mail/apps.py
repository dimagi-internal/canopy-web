from django.apps import AppConfig


class MailConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mail"
    label = "mail"

    def ready(self):
        # Moving an agent carries its mail (apps/agents/move.py). The move is
        # framework tier and cannot import this app, so the app registers itself.
        from apps.agents import move

        from .agent_move import participant

        move.register(participant)

        # Hosted mailboxes for agents, and domain sign-in, through the framework's seams
        # (apps/agents/mailbox.py, workspaces.services.register_verified_domain_owner).
        from apps.agents import mailbox
        from apps.workspaces import services as wsvc

        from . import domains

        mailbox.register(domains.MailboxProvider())
        wsvc.register_verified_domain_owner(domains.verified_domain_owner)
