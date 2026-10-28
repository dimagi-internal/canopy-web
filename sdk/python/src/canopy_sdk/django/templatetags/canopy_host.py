"""``{% load canopy_host %}`` then::

    {% canopy_panel resource="labs-marketplace://orgs" backing_tool="marketplace_orgs_get" visible_ids=slugs %}

at the end of the page. A page over sensitive data picks its own agent and requirements::

    {% canopy_panel resource=... agent="labsdata" runner_requirements=sensitive_data panel=wide %}

(``sensitive_data``/``wide`` from the view's context: ``["sensitive_data"]``, ``{"mode": "docked", "width": 560}``). Renders nothing unless ``CANOPY_HOST`` is configured —
a launcher that opens onto an error the page cannot explain is worse than none.
Override ``canopy_host/panel.html`` to restyle it.

A site sign-off, approved on the page itself (``canopy.signOff``)::

    {% canopy_sign_off asked.id then=collect_url %}

opens canopy's passkey dialog over the page for the request ``asked`` (from
``canopy_sdk.django.sign_offs.request``). When the person decides, the browser POSTs
``sign_off`` and ``state`` to ``then`` (with the page's CSRF token), where the host
reads the approval itself (``sign_offs.status``) — the page is never told it. Put
away undecided, the page stays: any element with ``data-canopy-sign-off-open`` opens
it again. With no id, it only defines ``window.canopySignOff(id) -> Promise``.
"""
from django import template

from ..pages import panel_context, sign_off_context

register = template.Library()


@register.inclusion_tag("canopy_host/panel.html", takes_context=True)
def canopy_panel(context, resource="", backing_tool="", visible_ids=None, filters=None, path="",
                 agent="", runner_requirements=None, panel=None):
    ctx = panel_context(context.get("request"), resource=resource, backing_tool=backing_tool,
                        visible_ids=visible_ids or (), filters=filters, path=path, agent=agent,
                        runner_requirements=runner_requirements or (), panel=panel)
    return {"canopy_panel": ctx, "csrf_token": context.get("csrf_token")}


@register.inclusion_tag("canopy_host/sign_off.html", takes_context=True)
def canopy_sign_off(context, sign_off="", then=""):
    ctx = sign_off_context(context.get("request"), sign_off=str(sign_off or ""), then=str(then or ""))
    return {"canopy_sign_off": ctx, "csrf_token": context.get("csrf_token")}
