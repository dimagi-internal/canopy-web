from django.contrib import admin

from .models import AgentSchedule, Runner, Turn, TurnEvent


@admin.register(Runner)
class RunnerAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "status", "last_heartbeat_at", "paired_at")
    list_filter = ("kind", "status")


@admin.register(Turn)
class TurnAdmin(admin.ModelAdmin):
    list_display = ("id", "agent", "origin", "status", "claimed_by", "created_at", "finished_at")
    list_filter = ("status", "origin")
    search_fields = ("id", "agent__slug")

    def get_queryset(self, request):
        # A turn on a personal project's task carries the project: its log stays its readers'.
        return super().get_queryset(request).exclude(raised_from_task__project__visibility="personal")


@admin.register(TurnEvent)
class TurnEventAdmin(admin.ModelAdmin):
    list_display = ("turn", "seq", "kind", "ts")
    list_filter = ("kind",)

    def get_queryset(self, request):
        return super().get_queryset(request).exclude(turn__raised_from_task__project__visibility="personal")


@admin.register(AgentSchedule)
class AgentScheduleAdmin(admin.ModelAdmin):
    list_display = ("name", "agent", "cron", "timezone", "enabled", "last_slot")
    list_filter = ("enabled", "agent")
    search_fields = ("name", "prompt")
