from django.contrib import admin

from .models import (
    Agent,
    AgentSkill,
    AgentSync,
    AgentTask,
    AgentTaskAction,
    RepoIdentity,
)
from .services import task_read_q


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


@admin.register(AgentSkill)
class AgentSkillAdmin(admin.ModelAdmin):
    list_display = ("agent", "name", "updated_at")
    list_filter = ("agent",)


@admin.register(AgentTask)
class AgentTaskAdmin(admin.ModelAdmin):
    list_display = ("agent", "title", "status", "owner", "assigned", "due", "updated_at")
    list_filter = ("agent", "status")

    def get_queryset(self, request):
        # Staff read no more of a personal project here than over the API.
        return super().get_queryset(request).filter(task_read_q(request.user))


@admin.register(AgentTaskAction)
class AgentTaskActionAdmin(admin.ModelAdmin):
    list_display = ("agent", "task", "action", "status", "created_at")
    list_filter = ("agent", "action", "status")

    def get_queryset(self, request):
        return super().get_queryset(request).filter(task_read_q(request.user, "task__"))
