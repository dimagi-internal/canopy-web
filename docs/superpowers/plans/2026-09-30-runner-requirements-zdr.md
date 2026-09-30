# Runner Requirements (ZDR first) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A connected site (host) can require, in its signed visitor assertions, that its visitors' conversations run only on runners whose owner has declared matching flags — `zdr` first.

**Architecture:** One vocabulary (`canopy_sdk.contract.RUNNER_FLAGS`). Runner owners declare flags (`harness.RunnerFlag` rows). Hosts send `canopy_runner_requirements` in the arrival assertion; canopy copies it onto the minted token, then onto every session that token creates or sends into (server-owned `metadata["runner_requirements"]`, union-only). Routing removes non-satisfying runners from the composed ladder and refuses them at claim — above pins, like `profile_q`. Placement, the unclaimable report, turn status and the host gateway all read the same two helpers.

**Tech Stack:** Django 5 + Django Ninja, pytest (SQLite in tests — no Postgres-only JSON lookups), React 19 + vitest, `dimagi-canopy` SDK in `sdk/python`.

**Spec:** `docs/superpowers/specs/2026-09-30-zdr-runners-design.md`

## Global Constraints

- The word is **ZDR**; never "secure" in code, API or UI.
- Known flags come ONLY from `canopy_sdk.contract.RUNNER_FLAGS = frozenset({"zdr"})`; canopy-web keeps no copy.
- Claim name: `canopy_runner_requirements` (a JSON list of strings). Host config key: `CANOPY_HOST["RUNNER_REQUIREMENTS"]`.
- Session metadata key: `runner_requirements` (sorted, deduped list); it is in `SERVER_OWNED_METADATA` and only ever grows.
- No fallback, no override: pins, bindings, strict/non-strict rules and the cascade grace all obey it.
- Unknown flag or malformed claim **fails closed** (arrival refused; malformed session value = unsatisfiable).
- Declaring flags is human-only: refuse `is_machine(request)`; gate = `_runner_admin_or_404` (pairer or `RunnerAdmin`).
- A route docstring is public API docs — rationale goes in `#` comments.
- Touching `sdk/python/src` requires bumping `sdk/python/pyproject.toml` version (0.5.0 → 0.6.0). Touching `frontend/packages/canopy-ui/src` requires bumping its `package.json` version (0.14.1 → 0.14.2).
- After any `schemas.py`/`api.py` change: `cd frontend && npm run gen:api:local` and commit `generated.ts`.
- Token colours only (no raw palette literals) in frontend.

## Review Focus

1. **A non-ZDR runner ranked above a ZDR one must not block it** — the cascade compares ranks; if the filter runs after the "better rank available" check, the ZDR box waits 60s for nothing. Test: `test_a_non_zdr_better_rank_does_not_block_the_zdr_runner` (Task 4).
2. **A pinned turn / a bound session on a non-ZDR box** — pins "trump everything" today; a reviewer will expect this one to be refused. Tests in Task 4 (`test_a_pin_to_a_non_zdr_runner_does_not_claim`, `test_a_session_bound_to_a_non_zdr_runner_waits`).
3. **The visitor's second visit** — a returning contact gets a new token and sends into an OLD session; the requirement must still hold and must not be downgraded by a token without it. Test: `test_a_later_token_without_the_claim_does_not_unstamp` (Task 6).
4. **The chat banner's "run on cloud" offer** — `available_cloud_runner` must not offer a non-ZDR box for a ZDR session (the offer would pin the turn somewhere claim then refuses → stuck forever). Test: `test_available_cloud_runner_skips_a_non_zdr_box` (Task 5).
5. **Malformed metadata** (`"zdr"` as a string, or `[1]`) must mean "nobody can take it", not "no requirements". Test: `test_a_malformed_value_is_unsatisfiable` (Task 2).

---

## File Structure

| File | Responsibility |
|---|---|
| `sdk/python/src/canopy_sdk/contract.py` | `RUNNER_REQUIREMENTS_CLAIM`, `RUNNER_FLAGS`, `parse_runner_requirements()` (shared parse, used by both halves) |
| `sdk/python/src/canopy_sdk/host/config.py` | `HostConfig.runner_requirements` |
| `sdk/python/src/canopy_sdk/django/conf.py` | read `RUNNER_REQUIREMENTS` |
| `sdk/python/src/canopy_sdk/host/signing.py` | add the claim |
| `apps/harness/runner_requirements.py` (new) | `requirements_of(turn)`, `requirements_of_session(session)`, `satisfies(flags, reqs)`, `describe(reqs)` |
| `apps/harness/models.py` | `RunnerFlag` model, `Runner.flags` |
| `apps/harness/api.py` + `schemas.py` | `PUT /runners/{id}/flags`, `RunnerOut.flags` |
| `apps/harness/services.py` | ladder filter, claim/coverage checks, unclaimable reason |
| `apps/canopy_sessions/services.py` | `_placeable_runner` check, `add_runner_requirements()`, `SERVER_OWNED_METADATA` |
| `apps/tokens/models.py` | `runner_requirements` on `DelegatedToken`/`ContactToken`, `AppCredential.last_runner_requirements` |
| `apps/tokens/contact_api.py` | parse claim at arrival; stamp on `start_session` and `send` |
| `apps/tokens/middleware.py` | `request.runner_requirements` |
| `apps/canopy_sessions/api.py` | stamp on member `create_session` + `send` |
| `apps/tokens/host_gateway.py` | refuse on an unsatisfying runner |
| `apps/harness/turn_status.py`, `apps/slack/status.py`, `frontend/packages/canopy-ui/src/chat/{protocol,turnStatus}.ts` | `requires` on the status + wording |
| `apps/tokens/connected_apps_api.py`, `frontend/src/pages/ConnectedAppsPage.tsx` | show the host's last requirement |
| `frontend/src/api/harness.ts`, `frontend/src/components/supervisor/RunnerFlags.tsx` (new), `RunnerDetail.tsx`, `components/agents/AgentRouting.tsx` | declare + badge |

---

### Task 1: SDK — the claim, the config, the signer

**Files:**
- Modify: `sdk/python/src/canopy_sdk/contract.py` (after the `# --- claims ---` block, ~line 101)
- Modify: `sdk/python/src/canopy_sdk/host/config.py:81-156` (`HostConfig`)
- Modify: `sdk/python/src/canopy_sdk/django/conf.py:~110-130` (the builder that reads `APP_NAME`)
- Modify: `sdk/python/src/canopy_sdk/host/signing.py:44-74` (`sign_visitor_assertion`)
- Modify: `sdk/python/pyproject.toml` (`version = "0.6.0"`), `sdk/python/README.md` (document the claim beside the arrival claims)
- Test: `sdk/python/tests/test_contract.py`, `sdk/python/tests/test_host_signing.py`, `sdk/python/tests/test_django.py`

**Interfaces:**
- Produces: `contract.RUNNER_REQUIREMENTS_CLAIM: str = "canopy_runner_requirements"`, `contract.RUNNER_FLAGS: frozenset[str] = frozenset({"zdr"})`, `contract.parse_runner_requirements(value) -> tuple[str, ...]` (raises `ValueError`), `HostConfig.runner_requirements: tuple[str, ...]`.

- [ ] **Step 1: Failing tests**

`sdk/python/tests/test_contract.py`:
```python
import pytest
from canopy_sdk import contract


def test_the_only_flag_today_is_zdr():
    assert contract.RUNNER_FLAGS == frozenset({"zdr"})


@pytest.mark.parametrize("value,expected", [
    (None, ()), ([], ()), (["zdr"], ("zdr",)), (["zdr", "zdr"], ("zdr",)),
])
def test_parse_runner_requirements_normalises(value, expected):
    assert contract.parse_runner_requirements(value) == expected


@pytest.mark.parametrize("value", ["zdr", ["ZDR"], ["nope"], [1], {"zdr": True}])
def test_parse_runner_requirements_refuses_anything_else(value):
    with pytest.raises(ValueError):
        contract.parse_runner_requirements(value)
```

`sdk/python/tests/test_host_signing.py` (use the file's existing config/key helpers; `decode_unverified` = whatever helper the file already uses to read claims — if none, `jwt` payload via `canopy_sdk.jose`):
```python
def test_a_host_requiring_zdr_says_so_in_every_assertion(host_config_factory):
    cfg = host_config_factory(runner_requirements=("zdr",))
    claims = read_claims(sign_visitor_assertion(cfg, "u-1"))
    assert claims[contract.RUNNER_REQUIREMENTS_CLAIM] == ["zdr"]


def test_no_requirement_means_no_claim(host_config_factory):
    claims = read_claims(sign_visitor_assertion(host_config_factory(), "u-1"))
    assert contract.RUNNER_REQUIREMENTS_CLAIM not in claims


def test_an_unknown_flag_is_refused_when_the_host_boots(host_config_factory):
    with pytest.raises(ValueError):
        host_config_factory(runner_requirements=("nope",))


def test_extra_cannot_forge_or_drop_the_requirement(host_config_factory):
    cfg = host_config_factory(runner_requirements=("zdr",))
    claims = read_claims(sign_visitor_assertion(
        cfg, "u-1", extra={contract.RUNNER_REQUIREMENTS_CLAIM: []}))
    assert claims[contract.RUNNER_REQUIREMENTS_CLAIM] == ["zdr"]
```
(If the file has no `host_config_factory`, add one wrapping the existing `HostConfig(...)` construction with `**overrides`.)

`sdk/python/tests/test_django.py`: with `CANOPY_HOST={..., "RUNNER_REQUIREMENTS": ["zdr"]}` (use the file's `override_settings` pattern), the built config has `runner_requirements == ("zdr",)`.

- [ ] **Step 2: Run, expect FAIL**

Run: `cd sdk/python && uv run pytest tests/test_contract.py tests/test_host_signing.py tests/test_django.py -q`
Expected: FAIL (`AttributeError: RUNNER_FLAGS`).

- [ ] **Step 3: Implement**

`contract.py`:
```python
#: What a host may require of the runner a visitor's conversation runs on. A
#: LIST, so a later requirement is a new flag rather than a new claim.
RUNNER_REQUIREMENTS_CLAIM = "canopy_runner_requirements"
#: Every flag a runner's owner may declare and a host may require. The ONLY list:
#: canopy-web imports it rather than keeping a copy.
RUNNER_FLAGS: frozenset[str] = frozenset({"zdr"})


def parse_runner_requirements(value) -> tuple[str, ...]:
    """The claim's value as a sorted, deduped tuple. ``None`` / ``[]`` → ``()``.

    Raises ``ValueError`` for anything else — a string, a non-string member, an
    unknown flag. Both halves fail CLOSED on it: a host at boot, canopy at arrival.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{RUNNER_REQUIREMENTS_CLAIM} must be a list of strings")
    unknown = set(value) - RUNNER_FLAGS
    if unknown:
        raise ValueError(f"unknown runner requirement(s): {', '.join(sorted(unknown))}")
    return tuple(sorted(set(value)))
```

`HostConfig`: add field `runner_requirements: Sequence[str] = ()` (after `probe`), and in `__post_init__`:
```python
        set_(self, "runner_requirements",
             contract.parse_runner_requirements(list(self.runner_requirements)))
```

`django/conf.py`, in the `HostConfig(...)` construction: `runner_requirements=tuple(cfg.get("RUNNER_REQUIREMENTS") or ()),`. Add `"RUNNER_REQUIREMENTS": ["zdr"],  # optional: only run my visitors on ZDR runners` to the example block at the top of the file.

`signing.py`, after building `claims` (so `extra` cannot override it):
```python
    if config.runner_requirements:
        claims[contract.RUNNER_REQUIREMENTS_CLAIM] = list(config.runner_requirements)
    else:
        claims.pop(contract.RUNNER_REQUIREMENTS_CLAIM, None)
```
(Import `contract` if not already.)

Bump `sdk/python/pyproject.toml` to `0.6.0`. README: under the arrival assertion's claims, add a row: `canopy_runner_requirements` — optional list; set via `CANOPY_HOST["RUNNER_REQUIREMENTS"]`; canopy runs the visitor's conversations only on runners whose owner declared every listed flag; unknown values are refused.

- [ ] **Step 4: Run, expect PASS**

Run: `cd sdk/python && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add sdk/python
git commit -m "feat(sdk): a host can require runner flags of its visitors' conversations (zdr)"
```

---

### Task 2: Runner flags — model and helpers

**Files:**
- Modify: `apps/harness/models.py` (new `RunnerFlag` after `RunnerAdmin` ~line 1003; `Runner.flags` property)
- Create: `apps/harness/migrations/0055_runnerflag.py` (via `makemigrations`)
- Create: `apps/harness/runner_requirements.py`
- Modify: `apps/harness/admin.py` (inline, optional one-liner)
- Test: `tests/test_runner_requirements.py` (new)

**Interfaces:**
- Consumes: `contract.RUNNER_FLAGS`, `contract.parse_runner_requirements` (Task 1).
- Produces: `RunnerFlag(runner, flag, declared_by, declared_at)`, related_name `declared_flags`; `Runner.flags -> frozenset[str]`; `runner_requirements.requirements_of_session(session) -> frozenset[str]`; `runner_requirements.requirements_of(turn) -> frozenset[str]`; `runner_requirements.satisfies(flags, reqs) -> bool`; `runner_requirements.describe(reqs) -> str`; constant `runner_requirements.METADATA_KEY = "runner_requirements"`; `runner_requirements.UNSATISFIABLE = "__malformed__"`.

- [ ] **Step 1: Failing tests** — `tests/test_runner_requirements.py`:
```python
"""Runner requirements: the vocabulary, a runner's declared flags, and reading a
session's requirements. Spec: docs/superpowers/specs/2026-09-30-zdr-runners-design.md"""
from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.harness import runner_requirements as rr
from apps.harness.models import Runner, RunnerFlag, Turn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def _session(meta=None):
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=a_workspace())
    return Session.objects.create(agent=agent, workspace=agent.workspace, title="c",
                                  metadata=meta or {})


def test_a_runner_has_no_flags_until_one_is_declared():
    r = Runner.objects.create(name="box", kind=Runner.CLOUD, capabilities={})
    assert r.flags == frozenset()
    RunnerFlag.objects.create(runner=r, flag="zdr")
    assert Runner.objects.get(pk=r.pk).flags == frozenset({"zdr"})


def test_a_flag_is_declared_once_per_runner():
    r = Runner.objects.create(name="box", kind=Runner.CLOUD, capabilities={})
    RunnerFlag.objects.create(runner=r, flag="zdr")
    with pytest.raises(IntegrityError):
        RunnerFlag.objects.create(runner=r, flag="zdr")


def test_no_metadata_means_no_requirements():
    assert rr.requirements_of_session(_session()) == frozenset()


def test_a_session_requirement_is_read():
    assert rr.requirements_of_session(_session({"runner_requirements": ["zdr"]})) == {"zdr"}


@pytest.mark.parametrize("bad", ["zdr", [1], {"zdr": 1}, ["nope"]])
def test_a_malformed_value_is_unsatisfiable(bad):
    reqs = rr.requirements_of_session(_session({"runner_requirements": bad}))
    assert not rr.satisfies(frozenset({"zdr"}), reqs)


def test_a_turn_without_a_session_has_no_requirements():
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=a_workspace())
    t = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="k")
    assert rr.requirements_of(t) == frozenset()


def test_satisfies_is_a_subset_check():
    assert rr.satisfies(frozenset(), frozenset())
    assert rr.satisfies(frozenset({"zdr"}), frozenset())
    assert rr.satisfies(frozenset({"zdr"}), frozenset({"zdr"}))
    assert not rr.satisfies(frozenset(), frozenset({"zdr"}))


def test_describe_names_the_flags_for_people():
    assert rr.describe(frozenset({"zdr"})) == "ZDR"
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_runner_requirements.py -q` → ImportError.

- [ ] **Step 3: Implement**

`models.py`, after `RunnerAdmin`:
```python
class RunnerFlag(models.Model):
    """One property a runner's OWNER vouches for (spec 2026-09-30-zdr-runners).

    canopy cannot verify any of these — `zdr` means "this box uses only
    zero-data-retention keys for Claude", which is not observable from here — so
    each row is an attestation and records WHO made it. Set only through the
    human-only flags route; nothing a runner reports can create one, or a box
    could promote itself.
    """

    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="declared_flags")
    flag = models.CharField(max_length=32)
    declared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, related_name="+")
    declared_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["runner", "flag"],
                                               name="one_flag_row_per_runner")]
```
(Confirm `settings` is imported in models.py; it is used by other FKs.)

On `Runner`, a property:
```python
    @property
    def flags(self) -> frozenset[str]:
        """What this box's owner has declared (`RunnerFlag`). Reads the prefetch
        cache when the caller used prefetch_related("declared_flags")."""
        return frozenset(f.flag for f in self.declared_flags.all())
```

`apps/harness/runner_requirements.py`:
```python
"""What a conversation requires of the runner it runs on, and whether a runner
satisfies it (spec 2026-09-30-zdr-runners).

The requirement is written by the HOST (a signed arrival claim), copied onto the
session's server-owned metadata, and read here — by claiming, the unclaimable
report, placement, turn status and the host gateway, so they cannot disagree.
"""
from __future__ import annotations

from canopy_sdk.contract import RUNNER_FLAGS, parse_runner_requirements

METADATA_KEY = "runner_requirements"
#: Stands in for a value that is not a valid requirement list. No runner can
#: declare it, so a corrupted row is unclaimable rather than unrestricted.
UNSATISFIABLE = "__malformed__"
_LABELS = {"zdr": "ZDR"}

__all__ = ["METADATA_KEY", "RUNNER_FLAGS", "describe", "requirements_of",
           "requirements_of_session", "satisfies"]


def requirements_of_session(session) -> frozenset[str]:
    raw = (getattr(session, "metadata", None) or {}).get(METADATA_KEY)
    try:
        return frozenset(parse_runner_requirements(raw))
    except ValueError:
        return frozenset({UNSATISFIABLE})


def requirements_of(turn) -> frozenset[str]:
    if not turn.chat_session_id:
        return frozenset()
    return requirements_of_session(turn.chat_session)


def satisfies(flags: frozenset[str], reqs: frozenset[str]) -> bool:
    return reqs <= flags


def describe(reqs) -> str:
    return ", ".join(_LABELS.get(r, r) for r in sorted(reqs))
```

Run `uv run python manage.py makemigrations harness -n runnerflag`.

- [ ] **Step 4: Run, expect PASS** — `uv run pytest tests/test_runner_requirements.py -q`.

- [ ] **Step 5: Commit**
```bash
git add apps/harness tests/test_runner_requirements.py
git commit -m "feat(harness): runners carry owner-declared flags; sessions carry requirements"
```

---

### Task 3: Declaring flags — API, RunnerOut, Event

**Files:**
- Modify: `apps/harness/schemas.py` (`RunnerOut.flags`, new `RunnerFlagsIn`)
- Modify: `apps/harness/api.py` (new route after `revoke_runner_admin` ~line 705)
- Modify: `apps/harness/services.py` (new `set_runner_flags`)
- Modify: `frontend/src/api/generated.ts` (regen)
- Test: `tests/test_runner_flags_api.py` (new)

**Interfaces:**
- Consumes: `RunnerFlag`, `Runner.flags` (Task 2); `_runner_admin_or_404`; `apps.common.views_debug.is_machine`.
- Produces: `PUT /api/harness/runners/{runner_id}/flags` body `{"flags": ["zdr"]}` → `RunnerOut`; operationId `set_runner_flags`; `services.set_runner_flags(runner, flags: set[str], *, by) -> tuple[set, set]` (added, removed); `RunnerOut.flags: list[str]`; `RunnerOut.known_flags: list[str]` (= sorted `contract.RUNNER_FLAGS`).

- [ ] **Step 1: Failing tests** — model the client setup on `tests/test_runner_admins.py` (copy its fixtures for an owner, an admin, a stranger, and a PAT-authed client). Tests:
```python
def test_the_pairer_declares_zdr(owner_client, runner):
    r = owner_client.put(f"/api/harness/runners/{runner.id}/flags",
                         {"flags": ["zdr"]}, content_type="application/json")
    assert r.status_code == 200 and r.json()["flags"] == ["zdr"]
    row = RunnerFlag.objects.get(runner=runner)
    assert row.declared_by.email == "jj@dimagi.com"

def test_a_runner_admin_may_declare_it(admin_client, runner): ...  # 200

def test_someone_who_cannot_administer_gets_404(stranger_client, runner): ...  # 404

def test_a_token_cannot_declare_it(pat_client, runner):
    # The runner authenticates with its pairer's PAT; that must not let a box vouch for itself.
    r = pat_client.put(...)
    assert r.status_code == 403
    assert not RunnerFlag.objects.exists()

def test_an_unknown_flag_is_422(owner_client, runner): ...

def test_the_set_is_replaced_and_withdrawal_deletes(owner_client, runner):
    put(["zdr"]); put([])
    assert not RunnerFlag.objects.filter(runner=runner).exists()

def test_redeclaring_keeps_the_original_declarer(owner_client, admin_client, runner):
    # replace-the-set stamps only ADDED flags
    put_as(owner_client, ["zdr"]); put_as(admin_client, ["zdr"])
    assert RunnerFlag.objects.get(runner=runner).declared_by.email == "jj@dimagi.com"

def test_declaring_records_an_event(owner_client, runner):
    put(["zdr"])
    assert Event.objects.filter(kind="runner.flag_declared").exists()  # when runner.workspace is set

def test_heartbeat_cannot_set_flags(pat_client, runner):
    # POST the runner's own heartbeat with {"flags": ["zdr"]} in the body (use the
    # heartbeat URL/payload shape from tests/test_harness_api*.py) → still no rows.
    assert not RunnerFlag.objects.exists()

def test_list_runners_includes_flags(owner_client, runner):
    RunnerFlag.objects.create(runner=runner, flag="zdr")
    rows = owner_client.get("/api/harness/runners/").json()
    row = next(x for x in rows if x["id"] == str(runner.id))
    assert row["flags"] == ["zdr"]
    assert row["known_flags"] == sorted(contract.RUNNER_FLAGS)   # the UI's only source
```
(Write each test fully in the file; `Event` is `apps.events.models.Event`. `pat_client` sends `HTTP_AUTHORIZATION=f"Bearer {raw}"` from `PersonalToken` as `test_runner_admins.py` does.)

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_runner_flags_api.py -q` → 404/405.

- [ ] **Step 3: Implement**

`schemas.py`:
```python
class RunnerFlagsIn(Schema):
    flags: list[str]
```
In `RunnerOut` add `flags: list[str] = []` with resolver:
```python
    # What this box's owner has declared about it (RunnerFlag) — e.g. `zdr`.
    @staticmethod
    def resolve_flags(obj) -> list[str]:
        return sorted(obj.flags)

    # Every flag an owner may declare — the contract's list, served per row (as
    # `expected_code_sha` is) so the UI draws one checkbox per known flag and
    # keeps no list of its own.
    known_flags: list[str] = []

    @staticmethod
    def resolve_known_flags(obj) -> list[str]:
        return sorted(contract.RUNNER_FLAGS)
```
(`from canopy_sdk import contract` in schemas.py.)

`services.py`:
```python
def set_runner_flags(runner: Runner, flags: set[str], *, by) -> tuple[set, set]:
    """Make the runner's declared flags exactly `flags`. Stamps only ADDED ones,
    so re-saving does not rewrite who first vouched. Returns (added, removed)."""
    current = set(runner.flags)
    added, removed = flags - current, current - flags
    with transaction.atomic():
        RunnerFlag.objects.filter(runner=runner, flag__in=removed).delete()
        for f in sorted(added):
            RunnerFlag.objects.create(runner=runner, flag=f, declared_by=by)
    return added, removed
```
(Import `RunnerFlag`.)

`api.py`:
```python
@router.put("/runners/{runner_id}/flags", response=RunnerOut,
            summary="Declare what this runner's owner vouches for")
def set_runner_flags(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerFlagsIn):
    """Replace the runner's declared flags. `zdr`: this box uses only
    zero-data-retention keys for Claude. canopy cannot check a declaration; it
    records who made it. A host may require a flag of every conversation its
    visitors hold, and those conversations then run only on runners declaring it.
    """
    # Human-only: a runner authenticates with its pairer's PAT, and a box must
    # never be able to vouch for itself. Same refusal as the agent-owner gates.
    if is_machine(request):
        raise HttpError(403, "a person must declare this, from the canopy web app")
    runner = _runner_admin_or_404(request, runner_id)
    try:
        wanted = set(contract.parse_runner_requirements(payload.flags))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    added, removed = services.set_runner_flags(runner, wanted, by=request.user)
    if runner.workspace_id and (added or removed):
        from apps.events import services as events

        events.record([
            {"source": "harness.runners", "kind": kind, "level": "info",
             "summary": f"{request.user.email} {verb} {flag} on {runner.name}",
             "payload": {"runner": str(runner.pk), "flag": flag, "by": request.user.email}}
            for kind, verb, names in (("runner.flag_declared", "declared", added),
                                      ("runner.flag_withdrawn", "withdrew", removed))
            for flag in sorted(names)
        ], workspace=runner.workspace)
    return Runner.objects.prefetch_related("declared_flags").get(pk=runner.pk)
```
Imports: `from canopy_sdk import contract`, `from apps.common.views_debug import is_machine` (check api.py's existing import of it first).

`list_runners`: add `.prefetch_related("declared_flags")` to its queryset.

Regen: `cd frontend && npm run gen:api:local`.

- [ ] **Step 4: Run, expect PASS** — `uv run pytest tests/test_runner_flags_api.py tests/test_runner_admins.py -q`.

- [ ] **Step 5: Commit**
```bash
git add apps/harness tests/test_runner_flags_api.py frontend/src/api/generated.ts
git commit -m "feat(harness): runner owners declare flags (zdr) — human-only, audited"
```

---

### Task 4: Routing obeys requirements — claim, ladder, coverage, unclaimable

**Files:**
- Modify: `apps/harness/services.py` — `load_assignment_rows` (~512), `assignment_rows_for` (~540), `_assignment_allows_for_agent` (~595), `_refined_allows` (~743), `_coverage` (~795), `turn_reach` (~840), `unclaimable_queued_turns` (~876), `claim_next_turn` (~963)
- Test: `tests/test_runner_requirements_routing.py` (new)

**Interfaces:**
- Consumes: `runner_requirements.{requirements_of, satisfies, describe}`, `Runner.flags`.
- Produces: `assignment_rows_for(agent_id, origin, actor, defaults, priorities, *, requires=frozenset())` — rows missing a required flag are dropped BEFORE ranks are renumbered. Unclaimable rows gain reason text naming the missing flags.

- [ ] **Step 1: Failing tests** — reuse the `fleet`, `_age`, `_offline` fixtures from `tests/test_unclaimable_source_rules.py` (copy them into the new file). Helpers:
```python
def _zdr(runner):
    RunnerFlag.objects.create(runner=runner, flag="zdr")

def _sessions(*runners):
    for r in runners:
        Runner.objects.filter(pk=r.pk).update(capabilities={"sessions": True})
        r.refresh_from_db()

def _zdr_turn(agent, key="c1", **kw):
    s = Session.objects.create(agent=agent, workspace=agent.workspace, title="chat",
                               metadata={"runner_requirements": ["zdr"]})
    return Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key=key, routing=Turn.ANY, **kw), s
```
Tests (each written out in full):
```python
def test_a_non_zdr_runner_does_not_claim_a_zdr_turn(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    _zdr_turn(a)
    assert services.claim_next_turn(laptop) is None

def test_a_zdr_runner_claims_it(fleet):  # cloud zdr, assigned rank 0 → claims

def test_a_non_zdr_better_rank_does_not_block_the_zdr_runner(fleet):
    # laptop rank 0 (online, not zdr), cloud rank 1 (zdr); turn is FRESH (no _age)
    ...
    assert services.claim_next_turn(cloud) is not None

def test_an_actor_rule_to_a_non_zdr_box_falls_to_a_zdr_default(fleet):
    # actor rule (canopy_web_chat, jj@dimagi.com) → laptop, non-strict; default → cloud(zdr)
    # turn enqueued_by=fleet["user"] → cloud claims, laptop does not

def test_a_strict_rule_to_a_non_zdr_box_waits(fleet):
    # strict source rule → laptop; cloud(zdr) in defaults → neither claims

def test_a_pin_to_a_non_zdr_runner_does_not_claim(fleet):
    # _zdr_turn(a, pinned_runner=laptop) → claim_next_turn(laptop) is None

def test_a_session_bound_to_a_non_zdr_runner_waits(fleet):
    # RunnerBinding(session, laptop) → laptop None; cloud(zdr, assigned) None too (stickiness)

def test_the_grace_never_promotes_a_non_zdr_runner(fleet):
    # _age(turn); laptop assigned rank 1 under an offline zdr cloud rank 0 → laptop None

def test_a_turn_without_requirements_is_unaffected(fleet):
    # plain session turn → laptop (non-zdr) claims as before

def test_unclaimable_reports_a_zdr_turn_with_no_zdr_runner_as_config(fleet):
    # laptop assigned (online, not zdr); _age(turn)
    stuck = services.unclaimable_queued_turns(fleet["user"])
    assert stuck[0]["kind"] == "config" and "ZDR" in stuck[0]["reason"]

def test_unclaimable_reports_an_offline_zdr_runner_as_offline(fleet):
    # cloud(zdr) assigned + _offline(cloud) → kind "offline"

def test_turn_reach_agrees_with_claiming(fleet):
    # laptop only, non-zdr → services.turn_reach(turn).kind == services.UNROUTED
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_runner_requirements_routing.py -q`.

- [ ] **Step 3: Implement**

a) `load_assignment_rows`: `.select_related("runner").prefetch_related("runner__declared_flags").order_by("rank")`.

b) `assignment_rows_for` — add keyword `requires: frozenset = frozenset()`; at the end, before `return`, filter both the early-return branch and the final list:
```python
    if not ladder:
        out = [r for _rank, r in base]
    else:
        ...existing composition building `out`...
    if requires:
        # A requirement is a FLOOR, applied to the composed list so a runner that
        # lacks it neither claims nor counts as a better-ranked blocker, and the
        # grace has nobody below the floor to promote (spec 2026-09-30).
        out = [r for r in out if rr.satisfies(r.flags, requires)]
    return list(enumerate(out))
```
(Restructure the early `return` into the `out` variable; keep the strict `truncated` logic unchanged. `from . import runner_requirements as rr` at module top.)

c) `_assignment_allows_for_agent`: pass `requires=rr.requirements_of(turn)`.

d) `_refined_allows(r, t, ...)`: first line
```python
    if not rr.satisfies(r.flags, rr.requirements_of(t)):
        return False  # above the pin and the binding, as in claim_next_turn
```
and pass `requires=rr.requirements_of(t)` into its `assignment_rows_for` call.

e) `_coverage`: runners arrive prefetched (step f); no other change (it calls `_refined_allows`).

f) `turn_reach` and `unclaimable_queued_turns`: add `.prefetch_related("declared_flags")` to both `Runner.objects...` queries.

g) `claim_next_turn`: before the loop, `my_flags = runner.flags`; first statement in `for turn in candidates:`
```python
        # Above the pin on purpose, like profile_q: a pin is a placement, never a
        # way past what the conversation's host requires of the box.
        if not rr.satisfies(my_flags, rr.requirements_of(turn)):
            continue
```

h) `unclaimable_queued_turns`, in the per-turn reason branch, before the `t.capability` branch:
```python
        reqs = rr.requirements_of(t)
        if reqs and t.pk not in claimable_ever:
            kind = "config"
            reason = (f"this conversation's site requires a {rr.describe(reqs)} runner, and "
                      f"no runner that serves it is declared {rr.describe(reqs)}")
        elif t.capability and ...
```

- [ ] **Step 4: Run, expect PASS** — `uv run pytest tests/test_runner_requirements_routing.py tests/test_source_rules.py tests/test_actor_rules.py tests/test_unclaimable_source_rules.py tests/test_claim_schedule_parity.py tests/test_harness_claim_sessions.py tests/test_harness_services.py -q`.

- [ ] **Step 5: Commit**
```bash
git add apps/harness/services.py tests/test_runner_requirements_routing.py
git commit -m "feat(routing): a conversation's runner requirements are a floor under every rung"
```

---

### Task 5: Placement only offers satisfying runners

**Files:**
- Modify: `apps/canopy_sessions/services.py:1053-1083` (`_placeable_runner`), `:1586` (`available_cloud_runner` — covered via `_placeable_runner`)
- Test: `tests/test_runner_requirements_placement.py` (new)

**Interfaces:**
- Consumes: `rr.requirements_of_session`, `rr.satisfies`, `Runner.flags`.

- [ ] **Step 1: Failing tests** (reuse the `fleet` fixture; both runners session-capable):
```python
def test_a_non_zdr_runner_is_not_a_placement_for_a_zdr_session(fleet):
    s = Session.objects.create(agent=fleet["agent"], workspace=fleet["ws"], title="c",
                               metadata={"runner_requirements": ["zdr"]})
    assert session_services._placeable_runner(s, str(fleet["laptop"].id)) is None

def test_a_zdr_runner_is(fleet): ...  # _zdr(cloud) → returns cloud

def test_available_cloud_runner_skips_a_non_zdr_box(fleet):
    # cloud online, session-capable, not zdr → available_cloud_runner(s) is None

def test_sending_with_an_explicit_non_zdr_placement_is_refused(fleet):
    with pytest.raises(ValueError):
        session_services.send_message(session=s, text="hi", user=fleet["user"],
                                      placement=str(fleet["laptop"].id))
```
(Check `_resolve_placement`'s docstring: an explicit unresolvable placement raises `ValueError` — the last test pins that.)

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** — in `_placeable_runner`, after the membership check:
```python
    from apps.harness import runner_requirements as rr

    # A box the conversation's host does not allow is no placement at all: a pin
    # to it would sit unclaimable forever (claim_next_turn refuses it above pins).
    if not rr.satisfies(runner.flags, rr.requirements_of_session(session)):
        return None
```

- [ ] **Step 4: Run, expect PASS** — plus `uv run pytest tests -q -k "place or transfer or move_queued"`.

- [ ] **Step 5: Commit**
```bash
git add apps/canopy_sessions/services.py tests/test_runner_requirements_placement.py
git commit -m "feat(sessions): placement offers only runners a conversation's host allows"
```

---

### Task 6: Arrival — the host's claim reaches the session

**Files:**
- Modify: `apps/tokens/models.py` — `DelegatedToken` (~406) and `ContactToken` (~655): `runner_requirements = models.JSONField(default=list, blank=True)`; `issue(...)` gains `runner_requirements=()`; `AppCredential`: `last_runner_requirements = models.JSONField(default=list, blank=True)`
- Create: `apps/tokens/migrations/0027_runner_requirements.py` (makemigrations)
- Modify: `apps/tokens/contact_api.py` — `contact_token` (~136), `start_session` (~401), `send` (~470)
- Modify: `apps/tokens/middleware.py:~78-121` — set `request.runner_requirements`
- Modify: `apps/canopy_sessions/services.py` — `SERVER_OWNED_METADATA` (~860), new `add_runner_requirements`
- Modify: `apps/canopy_sessions/api.py` — `create_session` (~193, the `acting_app` branch), `send` (~529)
- Test: `tests/test_runner_requirements_arrival.py` (new); extend `tests/test_session_metadata.py`

**Interfaces:**
- Consumes: `contract.parse_runner_requirements`, `contract.RUNNER_REQUIREMENTS_CLAIM`, `rr.METADATA_KEY`.
- Produces: `request.runner_requirements: tuple[str, ...]` (always set on the token paths; absent elsewhere → read with `getattr(request, "runner_requirements", ())`); `session_services.add_runner_requirements(session, reqs) -> None` (union, persist, never removes).

- [ ] **Step 1: Failing tests.** Build on `tests/test_sdk_round_trip.py`'s `host` / `site` fixtures and its `_arrive` helper (copy them, adding `runner_requirements` to the host config), or on the arrival fixtures in the contact-token tests (`grep -ln "contact-token" tests/`). Tests, written in full:
```python
def test_an_arrival_requiring_zdr_mints_a_token_that_carries_it(...):
    token = arrive(claims_extra={"canopy_runner_requirements": ["zdr"]})
    assert ContactToken.objects.get(...).runner_requirements == ["zdr"]

def test_a_member_arrival_carries_it_too(...):  # DelegatedToken path

def test_an_unknown_flag_refuses_the_arrival(...):
    r = arrive(claims_extra={"canopy_runner_requirements": ["nope"]})
    assert r.status_code == 400 and "runner" in r.json()["detail"]

def test_a_session_started_under_that_token_is_stamped(...):
    # POST /api/contact/sessions with the contact token
    assert Session.objects.get(...).metadata["runner_requirements"] == ["zdr"]

def test_a_member_session_created_through_the_site_is_stamped(...):
    # POST /api/canopy-sessions/ with the delegated token

def test_a_body_cannot_set_or_clear_it(...):
    # create with metadata={"runner_requirements": []} under a zdr token → still ["zdr"]
    # create with metadata={"runner_requirements": ["zdr"]} under a plain session login → 422 (server-owned)

def test_a_later_token_without_the_claim_does_not_unstamp(...):
    # arrive with zdr, start session; arrive again WITHOUT the claim; send into the same session
    assert session.metadata["runner_requirements"] == ["zdr"]

def test_a_later_token_with_the_claim_stamps_an_older_session(...):
    # session started without; re-arrive with zdr; send → stamped

def test_the_site_row_shows_what_the_host_last_sent(...):
    assert AppCredential.objects.get(...).last_runner_requirements == ["zdr"]
```
And in `tests/test_session_metadata.py`: add `"runner_requirements"` to whatever table pins each server-owned key to its owner (owner: `apps.harness.runner_requirements.METADATA_KEY`).

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

Models + `makemigrations tokens -n runner_requirements`. `issue`:
```python
    def issue(cls, *, app, contact, ttl_seconds, runner_requirements=()):
        ...create(..., runner_requirements=list(runner_requirements))
```
(same for `DelegatedToken.issue`).

`contact_token`, right after `app, claims = assertions.verify_for_issuer(...)` succeeds:
```python
    try:
        runner_reqs = contract.parse_runner_requirements(
            claims.get(contract.RUNNER_REQUIREMENTS_CLAIM))
    except ValueError as exc:
        # Refused, not ignored: ignoring it would drop a requirement the host
        # believes it made, and run its visitor wherever routing says.
        audit(event=EmbedAuditLog.EXCHANGE, request=request, app=app, ok=False,
              reason="bad_runner_requirements", detail=str(exc)[:200])
        raise HttpError(400, f"bad_runner_requirements: {exc}")
    if list(runner_reqs) != (app.last_runner_requirements or []):
        type(app).objects.filter(pk=app.pk).update(last_runner_requirements=list(runner_reqs))
```
Pass `runner_requirements=runner_reqs` to both `DelegatedToken.issue(...)` and `ContactToken.issue(...)`.

Middleware: in the `ctok` branch `request.runner_requirements = tuple(ctok.runner_requirements or ())`; in the `dtok` branch next to `request.delegated_app = dtok.app`, `request.runner_requirements = tuple(dtok.runner_requirements or ())`.

`canopy_sessions/services.py`: add `"runner_requirements"` to `SERVER_OWNED_METADATA`, and:
```python
def add_runner_requirements(session: Session, reqs) -> None:
    """Union `reqs` into the session's requirements. Never removes one: a
    conversation that held a host's data keeps the host's floor, whatever token
    touches it next (spec 2026-09-30-zdr-runners)."""
    from apps.harness import runner_requirements as rr

    reqs = set(reqs or ())
    if not reqs:
        return
    with transaction.atomic():
        s = Session.objects.select_for_update().get(pk=session.pk)
        meta = dict(s.metadata or {})
        current = set(rr.requirements_of_session(s))
        merged = sorted(current | reqs)
        if merged == sorted(current):
            return
        meta[rr.METADATA_KEY] = merged
        Session.objects.filter(pk=s.pk).update(metadata=meta)
    session.metadata = meta
```
(If `requirements_of_session` returned `{UNSATISFIABLE}`, the union keeps it — still unsatisfiable, which is the fail-closed answer.)

Stamp points:
- `contact_api.start_session`: after `metadata["embed_app"] = app.name`: `reqs = getattr(request, "runner_requirements", ()); if reqs: metadata["runner_requirements"] = list(reqs)`.
- `canopy_sessions/api.py::create_session`, inside `if acting_app is not None:` — same two lines.
- `contact_api.send` and `canopy_sessions/api.py::send`, after resolving `session`, before `send_message`: `session_services.add_runner_requirements(session, getattr(request, "runner_requirements", ()))` (in `canopy_sessions/api.py` the module is `services`).

- [ ] **Step 4: Run, expect PASS** — `uv run pytest tests/test_runner_requirements_arrival.py tests/test_session_metadata.py tests/test_sdk_round_trip.py tests/test_embed_session_provenance.py -q`, then `uv run pytest tests -q -k "contact or embed or delegat"`.

- [ ] **Step 5: Commit**
```bash
git add apps/tokens apps/canopy_sessions tests
git commit -m "feat(arrival): a host's runner requirements ride the token onto every session"
```

---

### Task 7: The gateway refuses an unsatisfying runner

**Files:**
- Modify: `apps/tokens/host_gateway.py:133-178` (`resolve`)
- Test: extend `tests/test_site_gateway.py`

- [ ] **Step 1: Failing test** (use the file's existing fixture that builds a site + grant + turn; set the session's metadata requirement and `turn.claimed_by` = a runner without `zdr`):
```python
def test_a_zdr_conversation_on_a_non_zdr_runner_is_refused(...):
    with pytest.raises(host_gateway.GatewayRefusal) as exc:
        host_gateway.resolve(str(turn.pk))
    assert exc.value.code == "runner_requirements"

def test_the_same_turn_on_a_zdr_runner_resolves(...): ...
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** — in `resolve`, add `"claimed_by"` to the `select_related`, and right after the `session is None` refusal:
```python
    from apps.harness import runner_requirements as rr

    # Defence in depth: routing should make this unreachable. This is where the
    # host's data enters a turn, so a routing bug fails as a refusal, not a leak.
    reqs = rr.requirements_of_session(session)
    if reqs and (turn.claimed_by is None or not rr.satisfies(turn.claimed_by.flags, reqs)):
        raise GatewayRefusal(
            "runner_requirements",
            f"this conversation must run on a {rr.describe(reqs)} runner, and this one is not")
```
(Check how `GatewayRefusal` exposes its code — `self.code` — and match the test.)

- [ ] **Step 4: Run, expect PASS** — `uv run pytest tests/test_site_gateway.py tests/test_sdk_round_trip.py -q`.

- [ ] **Step 5: Commit**
```bash
git add apps/tokens/host_gateway.py tests/test_site_gateway.py
git commit -m "feat(gateway): a host's data never reaches a runner its requirements exclude"
```

---

### Task 8: Turn status says why

**Files:**
- Modify: `apps/harness/turn_status.py` — `TurnStatus` (~76) field `requires: tuple[str, ...] = ()`, `as_dict`, `derive` (~174)
- Modify: `apps/slack/status.py:104-109`
- Modify: `frontend/packages/canopy-ui/src/chat/protocol.ts` (`TurnStatus` type: `requires?: string[]`), `frontend/packages/canopy-ui/src/chat/turnStatus.ts:118-135`, `frontend/packages/canopy-ui/package.json` (0.14.2)
- Test: `tests/test_turn_status.py`, Slack status test (`grep -ln "WAITING_RUNNER" tests/`), `frontend/packages/canopy-ui/src/chat/turnStatus.test.ts`

- [ ] **Step 1: Failing tests**

Python (`tests/test_turn_status.py`, reusing its turn/session builders):
```python
def test_a_queued_zdr_turn_carries_its_requirement():
    st = turn_status.derive(zdr_turn, reach=Reach(UNROUTED, []))
    assert st.requires == ("zdr",) and st.as_dict()["requires"] == ["zdr"]

def test_a_turn_without_one_carries_none():
    assert turn_status.derive(plain_turn, reach=Reach(UNROUTED, [])).as_dict()["requires"] == []
```
Slack: the UNROUTED line for a zdr turn contains `"no ZDR runner"`.

canopy-ui (`turnStatus.test.ts`, extend the existing `describe` for the statement function used at line ~118):
```ts
it('names the missing requirement when unrouted', () => {
  const s = statusStatement({ ...base, state: 'unrouted', agent_slug: 'ace', requires: ['zdr'] })
  expect(s.text).toContain('ZDR runner')
})
it('names it when its runners are offline', () => {
  const s = statusStatement({ ...base, state: 'waiting_runner', runners: ['cloud-1'], requires: ['zdr'] })
  expect(s.text).toContain('ZDR')
})
```
(Use the actual exported function name at line ~110 of turnStatus.ts.)

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_turn_status.py -q`; `cd frontend && npx vitest run packages/canopy-ui/src/chat/turnStatus.test.ts`.

- [ ] **Step 3: Implement**

`TurnStatus`:
```python
    #: What the conversation's host requires of the box (e.g. `zdr`). Only set
    #: while QUEUED: it is the reason a turn can sit with runners online.
    requires: tuple[str, ...] = ()
```
`as_dict`: `"requires": list(self.requires),`. `derive`: compute `requires = tuple(sorted(rr.requirements_of(turn))) if turn.status == Turn.QUEUED else ()` and pass `requires=requires`.

Slack:
```python
    elif st.state == ts.UNROUTED:
        if st.requires:
            line = (f":warning: Queued — this conversation needs a {rr.describe(st.requires)} "
                    f"runner, and none of {agent}'s runners is declared {rr.describe(st.requires)}.")
        else:
            ...existing line...
```
and for WAITING_RUNNER append `f" (it needs a {rr.describe(st.requires)} runner)"` when `st.requires`.

canopy-ui: add `requires?: string[]` to `TurnStatus` in `protocol.ts`. In `turnStatus.ts`:
```ts
const need = status.requires?.length ? status.requires.map((r) => r.toUpperCase()).join(', ') : ''
```
`unrouted` with `need`: `` `Queued — this conversation needs a ${need} runner, and none of ${status.agent_slug ?? 'its agent'}'s runners is declared ${need}.` ``; `waiting_runner` with `need`: append `` ` It needs a ${need} runner.` ``. Bump `package.json` to `0.14.2`.

- [ ] **Step 4: Run, expect PASS** — both suites; `uv run pytest tests -q -k "turn_status or slack_status"`.

- [ ] **Step 5: Commit**
```bash
git add apps/harness/turn_status.py apps/slack/status.py frontend/packages/canopy-ui tests
git commit -m "feat(status): a turn waiting for a ZDR runner says so, on every channel"
```

---

### Task 9: UI — declare, badge, and show the site's requirement

**Files:**
- Modify: `frontend/src/api/harness.ts` — `setRunnerFlags`
- Create: `frontend/src/components/supervisor/RunnerFlags.tsx` + `RunnerFlags.test.tsx`
- Modify: `frontend/src/components/supervisor/RunnerDetail.tsx:~200` (mount when `runner.can_administer`)
- Modify: `frontend/src/components/agents/AgentRouting.tsx:~366` (badge next to `r.runner_name`, from `fleet.find(f => f.id === r.runner_id)?.flags`)
- Modify: `apps/tokens/connected_apps_api.py` (`ConnectedAppOut.runner_requirements: list[str]` from `last_runner_requirements`), `frontend/src/pages/ConnectedAppsPage.tsx`, regen `generated.ts`

**Interfaces:**
- Consumes: `PUT /api/harness/runners/{runner_id}/flags`, `RunnerOut.flags`, `ConnectedAppOut.runner_requirements`.
- Produces: `setRunnerFlags(runnerId: string, flags: string[]): Promise<RunnerOut>`; `<RunnerFlags runner={RunnerOut} onChange={(r: RunnerOut) => void} />`.

- [ ] **Step 1: Failing test** — `RunnerFlags.test.tsx` (mock `@/api/harness` as `RunnerAdmins.test.tsx` does):
```tsx
it('declares zdr with the vouching sentence visible', async () => {
  render(<RunnerFlags runner={{ ...runner, flags: [] }} onChange={onChange} />)
  expect(screen.getByText(/You are vouching for this; canopy cannot check it/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('checkbox', { name: /ZDR/ }))
  expect(setRunnerFlags).toHaveBeenCalledWith(runner.id, ['zdr'])
})
it('withdraws it', async () => { /* flags: ['zdr'] → click → called with [] */ })
it('shows the server error', async () => { /* reject → error text rendered */ })
it('draws one checkbox per known flag from the server, not a local list', () => {
  render(<RunnerFlags runner={{ ...runner, flags: [], known_flags: ['zdr', 'eu'] }} onChange={onChange} />)
  expect(screen.getAllByRole('checkbox')).toHaveLength(2)
})
```
Plus an `AgentRouting` test: a fleet runner with `flags: ['zdr']` renders `data-testid="zdr-badge-<name>"`; and a ConnectedApps test: a site with `runner_requirements: ['zdr']` shows "Requires ZDR runners (declared by the host)".

- [ ] **Step 2: Run, expect FAIL** — `cd frontend && npx vitest run src/components/supervisor/RunnerFlags.test.tsx`.

- [ ] **Step 3: Implement**

`harness.ts`:
```ts
export async function setRunnerFlags(runnerId: string, flags: string[]): Promise<RunnerOut> {
  const res = await apiV2.PUT('/api/harness/runners/{runner_id}/flags', {
    params: { path: { runner_id: runnerId } },
    body: { flags },
  })
  return unwrap(res, 'setRunnerFlags')
}
```
`RunnerFlags.tsx` — one row per entry of `runner.known_flags` (the server's list; the component keeps only WORDING per flag: `const WORDS: Record<string, { label: string; sentence: string }> = { zdr: { label: 'ZDR', sentence: 'This box uses only zero-data-retention keys for Claude. You are vouching for this; canopy cannot check it.' } }`, falling back to `{ label: flag.toUpperCase(), sentence: 'You are vouching for this; canopy cannot check it.' }` for a flag it has no words for), a checkbox labelled by `label`, calls `setRunnerFlags` with the toggled set, calls `onChange` with the result, renders errors in `text-destructive`. Dense styling matching `RunnerAdmins.tsx` (tokens only). Mount in `RunnerDetail.tsx` next to `RunnerAdmins`: `{runner.can_administer && <RunnerFlags runner={runner} onChange={onRunnerChange} />}` (use whatever refresh callback `RunnerDetail` already has; if none, re-fetch via its existing loader).

`AgentRouting.tsx`, after the runner-name span:
```tsx
{fleet.find((f) => f.id === r.runner_id)?.flags?.includes('zdr') && (
  <span data-testid={`zdr-badge-${r.runner_name}`}
        className="rounded border border-info/30 bg-info/10 px-1 text-[10px] text-info">ZDR</span>
)}
```

`ConnectedAppOut`: `runner_requirements: list[str]` with `# What the host last required of its visitors' runners — display only; the signed claim is the authority.` set from `app.last_runner_requirements or []` wherever the out-row is built. Page: under the site's grant/probe lines, when non-empty: `Requires {reqs.map(r => r.toUpperCase()).join(', ')} runners (declared by the host)`.

Regen: `cd frontend && npm run gen:api:local`.

- [ ] **Step 4: Run, expect PASS** — `cd frontend && npx vitest run && npm run build`.

- [ ] **Step 5: Commit**
```bash
git add frontend apps/tokens/connected_apps_api.py
git commit -m "feat(ui): declare a runner ZDR; see it on routing rows and on the site"
```

---

### Task 10: End-to-end round trip, docs, PR

**Files:**
- Modify: `tests/test_sdk_round_trip.py` (new test)
- Modify: `CLAUDE.md` (one Design Decisions bullet), `docs/architecture/api-surface.md` (the new route), `docs/runbooks/connecting-a-system.md` (how a host turns it on)

- [ ] **Step 1: Failing test** — in `tests/test_sdk_round_trip.py`, using its `host`/`site` fixtures with the SDK host config built with `runner_requirements=("zdr",)`:
```python
def test_a_host_requiring_zdr_keeps_its_visitor_off_a_non_zdr_runner(site, host):
    token = _arrive(host)                       # the SDK's real signer, canopy's real arrival
    session = start a contact session with token (POST /api/contact/sessions)
    send a message with token (POST /api/contact/sessions/{id}/send) → turn
    laptop (session-capable, online, assigned, NOT zdr) → claim_next_turn is None
    cloud  (session-capable, online, assigned, zdr)    → claim_next_turn is turn
```
- [ ] **Step 2: Run** — `uv run pytest tests/test_sdk_round_trip.py -q` → should PASS already if Tasks 1–6 are right; if it fails, fix the task that is wrong, not the test.

- [ ] **Step 3: Docs.** `CLAUDE.md` Design Decisions bullet: **"A host may require runner flags of its visitors' conversations (ZDR first, 2026-09-30)."** — the host sets `CANOPY_HOST["RUNNER_REQUIREMENTS"]`; the signed `canopy_runner_requirements` claim rides the token onto the session (server-owned, union-only); owners declare flags per runner (`RunnerFlag`, human-only, audited); claim, placement, the unclaimable report, turn status and `site_call` all refuse a runner missing one, above pins; no fallback; canopy cannot verify a declaration. Point at the spec. `api-surface.md`: `PUT /api/harness/runners/{id}/flags`. Runbook: the two steps (owner declares the flag on the box; host sets the config and bumps `dimagi-canopy` to 0.6.0).

- [ ] **Step 4: Full suite** — `uv run pytest -q` and `cd frontend && npm run build && npx vitest run`. All green before continuing; report failures verbatim if not.

- [ ] **Step 5: Commit, push, PR with auto-merge**
```bash
git add -A && git commit -m "test+docs: runner requirements end to end; document the host switch"
git push -u origin HEAD
gh pr create --title "Runner requirements: a host can require ZDR runners for its visitors" --body "<summary + spec link + rollout steps>

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
gh pr merge <n> --auto
gh pr view <n> --json autoMergeRequest
```
