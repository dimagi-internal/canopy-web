"""canopy's LIVE PROBE of a Connected site's grant chain — nobody tests it by hand.

"Test connection" (`connection_test.py`) proves a site's documents, keys and
that it accepts canopy as a client, and stops exactly where a visitor starts: a
real grant needs an ID-JAG signed by the host's key. So the chain that matters
— issue, redeem, USE — was proven only when a visitor asked an agent for
something, and a break was found by a visitor who could not be helped.

A host with a probe identity (SDK ≥ 0.4.0, `canopy_sdk.host.ProbeIdentity`)
signs a real ID-JAG for ONE dedicated, low-privilege principal on canopy's
request. This module walks the whole chain with it, through the SAME code a
visitor's turn takes — nothing here is a parallel implementation:

1. **probe_issued** — `canopy_sdk.conformance.request_probe`: the host's
   metadata names its probe endpoint; canopy asks it as itself
   (`private_key_jwt` + DPoP), through `outbound.py`.
2. **live_grant_redeemed** — `host_grants.redeem`: the ID-JAG is checked
   against the site's registered keys, the token endpoint discovered, the
   jwt-bearer grant POSTed, the DPoP-bound token stored — exactly as at arrival.
3. **probe_tool_succeeds** — (a) the probe's tool, through `host_gateway` (the
   client `site_call` uses), as the probe principal.
4. **out_of_scope_refused** — (b) a tool outside the probe scope is not listed
   and is refused BY THE HOST (canopy's own ceiling is opened for the probe, so
   the refusal is the host's).
5. **dpop_required** — (c) the same call with no proof, a stranger's proof, and
   as a plain bearer, each refused.

The probe's grant is deleted when the probe ends: a probe token must not outlive
the probe. Probe traffic records no `host_grant.*` Event, so real-traffic health
(`traffic_health`) is never flattered by it.

**Where the result goes.** `record()` writes it on the site
(`last_probe_*`), raises an Event on a failure (coalesced per site, so a site
that stays broken is one row with a count) and on recovery, and pushes the
site's workspace owners on the transition — never on every failed run.

**When it runs.** On demand only — Test connection on a Connected site
(`POST …/connected-apps/{id}/test`). It ran every 30 minutes per site off the
runner reports until 2026-09-29; Jonathan wanted it on demand, and every probe
mints a real grant and makes real calls at a host, which is not free to do on a
clock nobody asked for.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)

#: One probe per site at a time (two owners can press Test connection at once).
SITE_LOCK_SECONDS = 120

EVENT_SOURCE = "tokens.live_probe"

#: The steps, in order, with the words the Connected sites page shows.
STEPS = {
    "probe_issued": "The site issues a probe grant",
    "live_grant_redeemed": "canopy redeems it (jwt-bearer + DPoP)",
    "probe_tool_succeeds": "The probe's tool runs as the probe user",
    "out_of_scope_refused": "A tool outside the scope is refused",
    "dpop_required": "The token is refused without a valid DPoP proof",
}


@dataclass
class Step:
    name: str
    status: str   # "pass" | "fail" | "skip"
    detail: str

    @property
    def label(self) -> str:
        return STEPS.get(self.name, self.name)


@dataclass
class ProbeReport:
    """`ok` is None when no verdict was reached: the site offers no probe (or
    canopy has no client keys) — "not set up", which is not a failure."""

    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> bool:
        self.steps.append(Step(name, status, detail[:500]))
        return status == "pass"

    @property
    def ok(self) -> bool | None:
        if any(s.status == "fail" for s in self.steps):
            return False
        if self.steps and all(s.status == "pass" for s in self.steps):
            return True
        return None

    @property
    def failing(self) -> Step | None:
        return next((s for s in self.steps if s.status == "fail"), None)

    @property
    def skipped(self) -> Step | None:
        return next((s for s in self.steps if s.status == "skip"), None)


# --- the probe ---------------------------------------------------------------------------


def run(app) -> ProbeReport:
    """Walk the whole chain for this site. Never raises; never records."""
    from canopy_sdk import conformance

    from . import client_identity, connection_test, host_gateway, host_grants, outbound

    report = ProbeReport()
    if not app.issues_host_grants():
        report.add("probe_issued", "skip", "this site does not let an agent act as a visitor")
        return report
    if not client_identity.configured():
        report.add("probe_issued", "skip", "this canopy has no OAuth client keys")
        return report

    def vet(url):
        outbound.check_url(url, what="probe endpoint")

    try:
        probe = conformance.request_probe(
            app.host_issuer, client_identity.credentials(),
            fetch_json=connection_test._fetch_json, post_form=connection_test._post_form, vet=vet)
    except conformance.ProbeError as exc:
        if exc.code == "no_probe_endpoint":
            report.add("probe_issued", "skip", "the site has no probe identity configured (SDK >= 0.4.0, "
                                               "CANOPY_HOST['PROBE'])")
        else:
            report.add("probe_issued", "fail", f"{exc.code}: {exc.message}")
        return report
    except Exception as exc:  # noqa: BLE001 - the type only; never a credential
        report.add("probe_issued", "fail", f"error: {type(exc).__name__}")
        return report
    report.add("probe_issued", "pass",
               f"principal {probe.subject!r}, scope {probe.scope!r}, tool {probe.tool!r}")

    grant = None
    try:
        try:
            grant = host_grants.redeem(app, probe.id_jag, subject=probe.subject)
        except host_grants.HostGrantError as exc:
            report.add("live_grant_redeemed", "fail", f"{exc.code}: {exc.message}")
            return report
        except Exception as exc:  # noqa: BLE001
            report.add("live_grant_redeemed", "fail", f"error: {type(exc).__name__}")
            return report
        if grant.scope.split() != [probe.scope]:
            report.add("live_grant_redeemed", "fail",
                       f"the token carries scope {grant.scope!r}, not the probe's {probe.scope!r}")
            return report
        report.add("live_grant_redeemed", "pass", f"scope {grant.scope!r}")
        _exercise(report, app, grant, probe, host_gateway)
    finally:
        if grant is not None:
            # A probe token must not outlive the probe.
            type(grant).objects.filter(pk=grant.pk).delete()
    return report


def _exercise(report: ProbeReport, app, grant, probe, host_gateway) -> None:
    from asgiref.sync import async_to_sync

    denied = probe.denied_tool or "canopy_probe_out_of_scope"
    try:
        # canopy's OWN narrowing (the ceiling) is set to exactly the two tools
        # the probe names, so what refuses the denied tool is the HOST.
        ctx = host_gateway.context_for_grant(app, grant, turn_id="probe", agent_slug="canopy-probe",
                                             ceiling=[probe.tool, denied])
    except host_gateway.GatewayRefusal as exc:
        report.add("probe_tool_succeeds", "fail", f"{exc.code}: {exc.message}")
        return

    # (a) the probe's own tool, as the probe principal.
    try:
        outcome = async_to_sync(host_gateway.probe_call)(ctx, probe.tool, probe.arguments)
    except host_gateway.GatewayRefusal as exc:
        report.add("probe_tool_succeeds", "fail", f"{exc.code}: {exc.message}")
        return
    if not report.add("probe_tool_succeeds", "pass" if outcome.kind == "ok" else "fail",
                      f"{probe.tool}: {outcome.kind}" + (f" — {outcome.detail}" if outcome.detail else "")):
        return

    # (b) a tool outside the scope: not listed, and refused when called.
    try:
        listed = [t["name"] for t in async_to_sync(host_gateway.list_tools)(ctx)]
    except host_gateway.GatewayRefusal as exc:
        report.add("out_of_scope_refused", "fail", f"listing failed — {exc.code}: {exc.message}")
        return
    called = async_to_sync(host_gateway.probe_call)(ctx, denied, {})
    note = "" if probe.denied_tool else " (the site names no denied_tool; a name no server offers was asked for)"
    if denied in listed:
        report.add("out_of_scope_refused", "fail", f"{denied} is listed to a {probe.scope} token")
        return
    if called.kind == "ok":
        report.add("out_of_scope_refused", "fail", f"{denied} RAN with a {probe.scope} token")
        return
    if called.kind in ("unreachable", "server_error"):
        report.add("out_of_scope_refused", "fail", f"{denied}: the site failed rather than refused "
                                                   f"({called.kind} {called.detail})")
        return
    report.add("out_of_scope_refused", "pass", f"{denied}: {called.kind}{note}")

    # (c) the same call without a valid DPoP proof.
    results = {}
    for mode in ("no_proof", "stranger_key", "bearer"):
        results[mode] = async_to_sync(host_gateway.probe_call)(ctx, probe.tool, probe.arguments, mode=mode)
    accepted = [m for m, o in results.items() if o.kind == "ok"]
    # Only a refusal of the CREDENTIAL (401/403) passes. A tool error or a 5xx
    # without a proof means the call got past (or crashed in) authentication —
    # both look like "refused" from a distance and neither is.
    wrong = [f"{m} ({o.kind} {o.detail})".strip() for m, o in results.items()
             if o.kind not in ("ok", "unauthorized")]
    if accepted:
        report.add("dpop_required", "fail", "the site ACCEPTED the token with " + ", ".join(accepted))
    elif wrong:
        report.add("dpop_required", "fail", "not refused as unauthorized: " + "; ".join(wrong))
    else:
        report.add("dpop_required", "pass",
                   "; ".join(f"{m}: {o.kind}{' ' + o.detail if o.detail else ''}" for m, o in results.items()))


# --- recording ------------------------------------------------------------------------------


def probe_and_record(app, *, trigger: str = "sweep") -> ProbeReport | None:
    """Run the probe and record it on the site. None when another probe of this
    site is already running (Test connection and the sweep can meet)."""
    lock = f"tokens:live_probe:{app.pk}"
    if not cache.add(lock, 1, timeout=SITE_LOCK_SECONDS):
        return None
    try:
        report = run(app)
        record(app, report, trigger=trigger)
        return report
    finally:
        cache.delete(lock)


def record(app, report: ProbeReport, *, trigger: str = "sweep") -> None:
    """Write the verdict on the site; Event + notify owners on a transition."""
    from .models import AppCredential

    previous = app.last_probe_ok
    failing = report.failing
    skipped = report.skipped
    now = timezone.now()
    fields = {
        "last_probe_at": now,
        "last_probe_ok": report.ok,
        "last_probe_step": (failing or skipped).name if (failing or skipped) else "",
        "last_probe_reason": ((failing or skipped).detail if (failing or skipped) else "")[:500],
    }
    AppCredential.objects.filter(pk=app.pk).update(**fields)
    for name, value in fields.items():
        setattr(app, name, value)

    if report.ok is False:
        _event(app, kind="host_probe.failed", level="error", key=f"{app.pk}:probe",
               summary=f"{app.name}: live probe failed at {failing.name} — {failing.detail}"[:500],
               payload={"site": app.name, "app_id": app.pk, "step": failing.name,
                        "reason": failing.detail[:300], "trigger": trigger})
        if previous is not False:
            _notify(app, title=f"{app.name}: agents cannot act for visitors",
                    body=f"Live probe failed — {failing.label}: {failing.detail}"[:240])
    elif report.ok is True and previous is False:
        _event(app, kind="host_probe.recovered", level="info", key="",
               summary=f"{app.name}: live probe passing again",
               payload={"site": app.name, "app_id": app.pk, "trigger": trigger})
        _notify(app, title=f"{app.name}: working again",
                body="The live probe passes again: agents can act for visitors on this site.")


def _event(app, **item) -> None:
    try:
        from apps.events import services as events

        events.record([{"source": EVENT_SOURCE, **item}], workspace=app.workspace)
    except Exception:  # noqa: BLE001 - bookkeeping must never fail a probe
        log.exception("could not record a live-probe event")


def _notify(app, *, title: str, body: str) -> int:
    """Push the site's workspace owners — the people who can fix a Connected
    site. The same channel canopy uses for "the fleet needs you" (`apps/push`);
    a site is not an agent, so there is no agent Inbox to put an ask in."""
    try:
        from django.contrib.auth import get_user_model

        from apps.push import services as push
        from apps.workspaces import services as wsvc

        # Owners of an ancestor workspace own this one too, and can fix its sites.
        owners = get_user_model().objects.filter(pk__in=wsvc.owner_user_ids(app.workspace_id))
        url = f"/w/{app.workspace_id}/settings/connected-apps"
        return sum(push.send_to_user(u, title, body, url) for u in owners)
    except Exception:  # noqa: BLE001
        log.exception("could not notify owners about a live-probe transition")
        return 0


# --- real traffic ---------------------------------------------------------------------------------


@dataclass
class TrafficHealth:
    last_redeemed_at: object = None
    last_site_call_at: object = None
    refusals_24h: int = 0


def traffic_health(app) -> TrafficHealth:
    """What real visitors' traffic says, from records canopy already keeps:
    the last redemption (`host_grant.redeemed` Events), the last successful
    `site_call` (the MCP audit, whose summary names the site's row), and the
    refusals seen in the last 24h — refused redemptions (coalesced Events, one
    per site+reason seen in the window) plus refused `site_call`s. The probe
    records none of these, so it cannot flatter them."""
    from apps.events.models import Event
    from apps.mcp.models import MCPAuditLog

    since = timezone.now() - timedelta(hours=24)
    grants = Event.objects.filter(workspace_id=app.workspace_id, source="tokens.host_grants",
                                  payload__site=app.name)
    last_redeemed = (grants.filter(kind="host_grant.redeemed").order_by("-last_seen_at")
                     .values_list("last_seen_at", flat=True).first())
    refused_grants = grants.filter(kind="host_grant.refused", last_seen_at__gte=since).count()
    marker = f" app={app.pk} "
    calls = MCPAuditLog.objects.filter(tool="site_call", args_summary__contains=marker)
    last_call = (calls.filter(ok=True, args_summary__contains="is_error=False").order_by("-created_at")
                 .values_list("created_at", flat=True).first())
    refused_calls = calls.filter(ok=False, created_at__gte=since).count()
    return TrafficHealth(last_redeemed_at=last_redeemed, last_site_call_at=last_call,
                         refusals_24h=int(refused_grants) + refused_calls)
