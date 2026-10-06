from django.contrib import admin

from .models import BetaRequest


@admin.register(BetaRequest)
class BetaRequestAdmin(admin.ModelAdmin):
    list_display = ("email", "created_at", "notify_result")
    search_fields = ("email", "reason")
    readonly_fields = ("email", "reason", "created_at", "client_ip", "user_agent", "notify_result")
