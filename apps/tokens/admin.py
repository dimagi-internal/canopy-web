from django.contrib import admin
from django.utils import timezone

from .models import AppCredential, AppCredentialAgent, EmbedAuditLog, PersonalToken


@admin.register(PersonalToken)
class PersonalTokenAdmin(admin.ModelAdmin):
    list_display = ("label", "user", "created_at", "last_used_at", "revoked_at")
    list_filter = ("revoked_at",)
    search_fields = ("label", "user__email", "user__username")
    readonly_fields = ("token_hash", "created_at", "last_used_at")
    actions = ["revoke_selected"]

    @admin.action(description="Revoke selected tokens")
    def revoke_selected(self, request, queryset):
        n = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.message_user(request, f"Revoked {n} token(s).")


class AppCredentialAgentInline(admin.TabularInline):
    """Read-only view of which agents an app may offer."""

    model = AppCredentialAgent
    extra = 0
    can_delete = False
    verbose_name = "allowed agent"
    verbose_name_plural = "allowed agents"

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AppCredential)
class AppCredentialAdmin(admin.ModelAdmin):
    """Read-only. Embedding apps are managed on **Connected sites**.

    They were managed here once, and that was wrong twice over. Operationally
    the admin is staff-only, so the person who wants to embed an agent could
    not do it — and there is nowhere to run the equivalent management command
    either (`EnableExecuteCommand` is off on the service and the database is
    VPC-internal). Conceptually, "connect my site to canopy" is something a
    person does, not a row an administrator edits: the fields are all
    fail-closed and silent, so a form with no explanation beside it produces a
    widget that never appears and no way to find out why.

    The surface is `/w/{workspace}/connected-apps`
    (`apps/tokens/connected_apps_api.py`), owned by workspace owners like every
    other tenant-admin page. This stays registered because inspecting a row is
    genuinely useful when something is not working — and stays READ-ONLY so it
    cannot quietly become the management path again, which is exactly how it
    became one the first time.
    """

    inlines = [AppCredentialAgentInline]
    list_display = (
        "name",
        "workspace",
        "domains_display",
        "origins_display",
        "agents_display",
        "created_at",
        "last_used_at",
        "revoked_at",
    )
    list_filter = ("revoked_at", "workspace")
    search_fields = ("name",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # True, with every field read-only: False would hide the detail page
        # entirely, and looking at a row is the reason this is still here.
        return True

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    @admin.display(description="delegation domains")
    def domains_display(self, obj):
        return ", ".join(obj.allowed_delegation_domains or []) or "— none (cannot mint)"

    @admin.display(description="frame origins")
    def origins_display(self, obj):
        valid = obj.frame_origins()
        stored = obj.allowed_frame_origins or []
        if not stored:
            return "— none (widget 404s)"
        suffix = f"  ({len(stored) - len(valid)} invalid, ignored)" if len(valid) != len(stored) else ""
        return ", ".join(valid) + suffix

    @admin.display(description="agents")
    def agents_display(self, obj):
        # `agent__slug`, not `agent_id`: Agent's PK is an auto integer (the slug
        # is unique but not the primary key), so the id is neither joinable nor
        # meaningful to read.
        names = list(obj.allowed_agents.values_list("agent__slug", flat=True))
        return ", ".join(names) or "— none (offers nothing)"


@admin.register(EmbedAuditLog)
class EmbedAuditLogAdmin(admin.ModelAdmin):
    """Read the embed audit trail. Append-only, so nothing here can write.

    Registered because a trail nobody can read is not a trail — this is the
    only place the question "did anything act as me, and what let it" can be
    asked today. A queryable product surface would be better and is not built
    yet; the filters below are what make this usable meanwhile.
    """

    list_display = ("created_at", "event", "ok", "reason", "app_name",
                    "subject_email", "actor", "client_ip")
    list_filter = ("event", "ok", "reason", "app_name")
    search_fields = ("app_name", "subject_email", "detail", "client_ip")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return True  # the detail VIEW; every field is read-only below

    def has_delete_permission(self, request, obj=None):
        # An audit trail somebody can prune is not one.
        return False

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]
