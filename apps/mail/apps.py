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

        # Every agent gets <slug>@<domain> where a domain says so: kept in sync as agents
        # are created and moved and workspaces re-parented (auto_addresses.py).
        from django.db.models.signals import post_save

        from apps.agents.models import Agent
        from apps.workspaces.models import Workspace

        from . import auto_addresses

        post_save.connect(auto_addresses.on_agent_saved, sender=Agent,
                          dispatch_uid="mail.agent_addresses.agent")
        post_save.connect(auto_addresses.on_workspace_saved, sender=Workspace,
                          dispatch_uid="mail.agent_addresses.workspace")
        wsvc.register_verified_domain_owner(domains.verified_domain_owner)
