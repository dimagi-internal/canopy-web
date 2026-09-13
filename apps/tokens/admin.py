from django import forms
from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    AppCredential,
    AppCredentialAgent,
    PersonalToken,
    is_valid_frame_origin,
)


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


class AppCredentialForm(forms.ModelForm):
    """Validation lives here, not in `save_model`.

    A `ValidationError` raised from `save_model` is not caught by the admin —
    it becomes a 500 rather than a re-rendered form with the message attached
    to the field. Found by a test that expected a 200 with errors and got an
    exception.
    """

    class Meta:
        model = AppCredential
        fields = "__all__"

    def clean_allowed_frame_origins(self):
        """Reject a bad origin at the form, not at the header.

        `frame_origins()` already filters on read, so a bad value cannot
        produce a permissive policy — but it would silently not apply, and an
        admin who typed it would believe the grant was in force. A wildcard is
        the case that matters: it would restore exactly the exposure
        `X-Frame-Options: DENY` was preventing.
        """
        origins = self.cleaned_data.get("allowed_frame_origins") or []
        if not isinstance(origins, list):
            raise forms.ValidationError("Expected a JSON list of origins.")
        bad = [o for o in origins if not is_valid_frame_origin(o)]
        if bad:
            raise forms.ValidationError(
                f"Not valid origins: {bad}. Use scheme + host + optional port "
                "(https://labs.connect.dimagi.com, http://localhost:8000) — no "
                "path, no wildcard. A wildcard is refused on purpose: "
                "frame-ancestors exists to enumerate the set."
            )
        return origins


class AppCredentialAgentInline(admin.TabularInline):
    """Which agents this app may offer.

    Inline rather than a separate page because it is never edited
    independently — "register connect-labs" and "decide what it can offer" are
    one operational act, and the whole point of the allowlist is that it is
    explicit server-side data rather than a host's own settings constant.
    """

    model = AppCredentialAgent
    extra = 1
    autocomplete_fields = ("agent",)
    verbose_name = "allowed agent"
    verbose_name_plural = "allowed agents"


@admin.register(AppCredential)
class AppCredentialAdmin(admin.ModelAdmin):
    """Register an embedding application, its origins, and its agents.

    **Why this is in the admin at all.** Every one of these facts had a
    management command and no other route, and there is nowhere to run one:
    the deployed service has `EnableExecuteCommand` off (so no `aws ecs
    execute-command`) and the RDS instance is VPC-internal (so no laptop
    shell). Registering an embedding app was therefore not actually possible
    on a deployment. The commands remain for scripted setup; this is the door
    a human can use.
    """

    form = AppCredentialForm
    inlines = [AppCredentialAgentInline]
    list_display = (
        "name",
        "domains_display",
        "origins_display",
        "agents_display",
        "created_at",
        "last_used_at",
        "revoked_at",
    )
    list_filter = ("revoked_at",)
    search_fields = ("name",)
    actions = ["revoke_selected"]

    fields = (
        "name",
        "allowed_delegation_domains",
        "allowed_frame_origins",
        "provision_workspace",
        "provision_role",
        "created_by",
        "created_at",
        "last_used_at",
        "revoked_at",
    )
    # `token_hash` is deliberately absent from `fields` entirely, not merely
    # read-only: the raw value is shown once at creation and is unrecoverable
    # afterwards, so a hash in the form is an invitation to paste something
    # into it.
    readonly_fields = ("created_by", "created_at", "last_used_at", "revoked_at")

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

    def save_model(self, request, obj, form, change):
        """Mint on create, and show the raw credential exactly once.

        `AppCredential.create_credential` is the only writer that generates a
        token, so creating through the admin has to go through it rather than
        a bare `save()` — otherwise the row would have an empty `token_hash`
        and authenticate nothing, silently.
        """
        if change:
            obj.save()
            return

        raw, created = AppCredential.create_credential(
            name=obj.name,
            domains=obj.allowed_delegation_domains or [],
            created_by=request.user,
            provision_workspace=obj.provision_workspace,
            provision_role=obj.provision_role,
        )
        created.allowed_frame_origins = obj.allowed_frame_origins or []
        created.save(update_fields=["allowed_frame_origins"])
        # Carry the real pk back so the inline rows attach to it.
        obj.pk = created.pk

        self.message_user(
            request,
            f"Credential for {created.name!r} created. Copy it now — it is not "
            f"stored and cannot be shown again:  {raw}",
            level=messages.WARNING,
        )

    @admin.action(description="Revoke selected credentials")
    def revoke_selected(self, request, queryset):
        n = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.message_user(
            request,
            f"Revoked {n} credential(s). Their embed shells now 404 and their "
            "existing delegated tokens stop working on their next request.",
        )
