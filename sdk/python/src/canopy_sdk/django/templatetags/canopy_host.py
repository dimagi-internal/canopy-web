"""``{% load canopy_host %}`` then::

    {% canopy_panel resource="labs-marketplace://orgs" backing_tool="marketplace_orgs_get" visible_ids=slugs %}

at the end of the page. Renders nothing unless ``CANOPY_HOST`` is configured —
a launcher that opens onto an error the page cannot explain is worse than none.
Override ``canopy_host/panel.html`` to restyle it.
"""
from django import template

from ..pages import panel_context

register = template.Library()


@register.inclusion_tag("canopy_host/panel.html", takes_context=True)
def canopy_panel(context, resource="", backing_tool="", visible_ids=None, filters=None, path=""):
    ctx = panel_context(context.get("request"), resource=resource, backing_tool=backing_tool,
                        visible_ids=visible_ids or (), filters=filters, path=path)
    return {"canopy_panel": ctx, "csrf_token": context.get("csrf_token")}
