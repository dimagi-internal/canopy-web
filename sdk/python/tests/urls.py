from django.http import JsonResponse
from django.shortcuts import render
from django.urls import include, path

from canopy_sdk.django.views import jwt_bearer_view


def toolkit_token_view(request):
    """Stands in for django-oauth-toolkit's TokenView."""
    return JsonResponse({"error": "unsupported_grant_type", "from": "toolkit"}, status=400)


def network(request):
    return render(request, "page.html", {"ids": ["llo-a", "llo-b"]})


def elsewhere(request):
    return render(request, "page.html", {"ids": []})


def data(request):
    return render(request, "data_page.html", {
        "reqs": ["sensitive_data"],
        "panel": {"mode": "docked", "width": 560, "expandable": True, "initial_display": "expanded"},
    })


def sign_off_page(request):
    return render(request, "sign_off_page.html", {"sign_off_id": "so_abcdefghijklmnop"})


urlpatterns = [
    path("canopy/", include("canopy_sdk.django.urls")),
    path("o/token/", jwt_bearer_view(toolkit_token_view)),
    path("marketplace/network/", network, name="network"),
    path("elsewhere/", elsewhere, name="elsewhere"),
    path("sign-off/", sign_off_page, name="sign_off_page"),
    path("data/visits/", data, name="data"),
]
