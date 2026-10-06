from django.contrib import admin

from .models import (
    Agent,
    AgentSkill,
    AgentSync,
    AgentTask,
    AgentTaskCommand,
    AgentWorkProduct,
    RepoIdentity,
)


class RepoIdentityInline(admin.TabularInline):
    """Repos whose agentless project turns run as this agent's GitHub identity."""

    model = RepoIdentity
    extra = 0
    fields = ("repo_slug",)


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "email", "owner", "updated_at")
    search_fields = ("slug", "name", "email")
    inlines = [RepoIdentityInline]


@admin.register(RepoIdentity)
class RepoIdentityAdmin(admin.ModelAdmin):
    list_display = ("repo_slug", "agent", "updated_at")
    search_fields = ("repo_slug", "agent__slug")
    autocomplete_fields = ("agent",)


@admin.register(AgentSync)
class AgentSyncAdmin(admin.ModelAdmin):
    list_display = ("agent", "title", "period_end", "source", "created_at")
    list_filter = ("agent", "source")


@admin.register(AgentWorkProduct)
class AgentWorkProductAdmin(admin.ModelAdmin):
    list_display = ("agent", "title", "kind", "created_at")
    list_filter = ("agent", "kind")


@admin.register(AgentSkill)
class AgentSkillAdmin(admin.ModelAdmin):
    list_display = ("agent", "name", "updated_at")
    list_filter = ("agent",)


@admin.register(AgentTask)
class AgentTaskAdmin(admin.ModelAdmin):
    list_display = ("agent", "title", "status", "owner", "assigned", "due", "updated_at")
    list_filter = ("agent", "status")


@admin.register(AgentTaskCommand)
class AgentTaskCommandAdmin(admin.ModelAdmin):
    list_display = ("agent", "kind", "task", "status", "created_by", "created_at")
    list_filter = ("agent", "kind", "status")
