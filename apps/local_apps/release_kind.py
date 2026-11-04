"""Kind: release one local app version to Canopy's store.

The approval names exact bytes by sha256, so what is approved is what ships. It
needs a superuser because the catalog is Canopy's and Canopy is vouching for code
that runs as the people who install it; opening this up needs a reviewer role.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from pydantic import Field

from apps.agents.models import Agent
from apps.approvals.kinds import Described, ExecError, Kind, KindError, register
from apps.common.schemas import StrictModel
from apps.workspaces.links import scoped_url


class Params(StrictModel):
    submission: int
    sha256: str = Field(min_length=64, max_length=64)


def _facts_lines(facts: dict) -> list[str]:
    out = [f"{len(facts.get('files') or [])} files"]
    for label, name in (
        ("Reaches the network", "network"),
        ("Starts processes", "process"),
        ("Uses risky features", "risky"),
    ):
        if facts.get(name):
            out.append(f"{label}: {', '.join(sorted(facts[name]))}")
    if facts.get("unparsed"):
        out.append(f"Could not read: {', '.join(facts['unparsed'])}")
    return out


@register
class LocalAppRelease(Kind):
    slug = "local_app.release"
    label = "Release an app that runs on people's computers"
    params_model = Params
    needs = "a Canopy superuser, who did not submit it"
    does = (
        "Lists this exact version in Canopy's store. People who have the app get it at their next Start, "
        "unless what it reads or sends changed, when they're asked first."
    )

    def _submission(self, p: Params):
        from .models import LocalAppSubmission  # noqa: PLC0415 — loaded from AppConfig.ready's import chain

        sub = LocalAppSubmission.objects.select_related("workspace", "submitted_by").filter(pk=p.submission).first()
        if sub is None or sub.sha256 != p.sha256:
            raise KindError("that submission is not there")
        return sub

    def describe(self, p: Params) -> Described:
        sub = self._submission(p)
        m = sub.manifest
        plan = [f"Exact package: sha256 {sub.sha256}", *_facts_lines(sub.facts)]
        plan += [f"Says it reads: {r}" for r in m["disclosure"]["reads"]]
        plan += [f"Says it sends: {s}" for s in m["disclosure"]["sends"]]
        plan.append(f"Read the code and the changes: {scoped_url(sub.workspace, f'/apps/local/review/{sub.pk}')}")
        return Described(
            workspace=sub.workspace_id,
            title=f"Release {m['name']} {sub.version} to Canopy's app store",
            summary=m["description"] or m["name"],
            plan=plan,
            scopes=[f"workspace:{sub.workspace_id}"],
            about_email=(sub.submitted_by.email if sub.submitted_by else ""),
        )

    def may_approve(self, user, p: Params, d: Described) -> bool:
        if not user.is_superuser:
            return False
        return not (d.about_email and (user.email or "").lower() == d.about_email.lower())

    def approvers(self, p: Params, d: Described) -> list:
        # A superuser's authority doesn't come from the submitter's workspace.
        bots = Agent.objects.filter(user__isnull=False).values_list("user_id", flat=True)
        rows = get_user_model().objects.filter(is_superuser=True, is_active=True).exclude(pk__in=bots).order_by("pk")
        return [u for u in rows if self.may_approve(u, p, d)]

    def recipients(self, p: Params, d: Described) -> list:
        return self.approvers(p, d)

    def execute(self, user, p: Params, request) -> dict:
        from . import services  # noqa: PLC0415 — services imports approvals, which holds this registry

        try:
            sub = services.release(self._submission(p))
        except services.Refused as exc:
            raise ExecError(str(exc)) from exc
        return {"key": sub.key, "version": sub.version, "sha256": sub.sha256}
