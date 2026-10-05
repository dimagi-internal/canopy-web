from django.contrib import admin

from .models import RetentionRule, RetentionSweep


@admin.register(RetentionRule)
class RetentionRuleAdmin(admin.ModelAdmin):
    list_display = ("id", "workspace", "kind", "source", "principal", "agent", "keep_days", "updated_at")
    list_filter = ("kind", "source", "principal", "workspace")
    raw_id_fields = ("agent",)
    readonly_fields = ("created_by", "created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(RetentionSweep)
class RetentionSweepAdmin(admin.ModelAdmin):
    list_display = ("started_at", "finished_at", "applied", "trigger", "error")
    list_filter = ("applied", "trigger")
    readonly_fields = ("started_at", "finished_at", "applied", "trigger", "counts", "error")

    def has_add_permission(self, request):
        return False
