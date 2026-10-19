"""Bare Django views for the harness (outside the Ninja API)."""
from __future__ import annotations

from django.http import HttpRequest, HttpResponseRedirect
from django.views.decorators.http import require_GET

from .models import RunnerAppPreference


@require_GET
def runner_app_home(request: HttpRequest) -> HttpResponseRedirect:
    """`/runner-app/` — where the Canopy Runner desktop app's window opens: the
    page the person chose in Settings (`RunnerAppPreference`), else the supervisor.
    Signed out goes to the supervisor too, which sends you to sign in."""
    page = RunnerAppPreference.PAGES[RunnerAppPreference.opens_on_for(request.user)]
    return HttpResponseRedirect(request.META.get("SCRIPT_NAME", "") + page)
