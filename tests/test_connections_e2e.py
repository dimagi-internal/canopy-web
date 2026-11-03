"""The real canopy CLI against the real canopy-web, over real HTTP: nothing about a
connection between them is a stand-in.

`live_server` runs canopy-web (real routes, gates, DB). Each machine is a subprocess of the
CLI's own interpreter with its own `HOME` and `CANOPY_DEVICE_KEY_DIR`, a software device key
the CLI made itself and enrolled through the real route. The owner's browser is Python:
it talks to the real routes with a real session, and does its crypto with the normative
reference (`docs/vault/zk-v1/connections_v1_reference.py`).

Faked, and nothing else:
- the OAuth provider on the CLI's side, by replacing `providers.base.post_form` in a wrapper
  around the CLI's real `main` (`WRAPPER` below). Stateful across processes: it rotates
  Salesforce refresh tokens and refuses a rotated-away one, so a stale token is a failure.
- the OAuth provider on canopy-web's side, at the `requests` boundary during connect
  (`FakeSF`, `FakeGoogle`), since the server really does exchange the code.
- two browser acts that need a person's hand: the passkey step-up (the session is stamped
  as `mark_stepped_up` stamps it) and approving a device key (the row is set active).

The wrapper also refuses every socket that is not loopback, and logs the attempt: no run of
this suite can reach a real provider or canopy.dimagi.com.

It reads as one story, so it is one test: `Story` records each scenario and carries on past
a failure, and the test fails at the end listing them. Run:

    uv run pytest tests/test_connections_e2e.py --nomigrations -s

`CANOPY_CLI_ROOT` names the CLI's checkout (default: `canopy-connections-zk` beside this one,
with its `.venv`); the test skips without it. `E2E_RESULTS` names a file for the results.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import textwrap
import time
import traceback
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.forms.models import model_to_dict
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin, AgentVaultEndorsement
from apps.connections import recipients as web_recipients
from apps.connections import tokens as web_tokens
from apps.connections.models import Connection
from apps.connections.providers import google as web_google
from apps.events.models import Event
from apps.harness.models import Runner, RunnerAccessToken, RunnerAssignment, RunnerDeviceKey, Turn
from apps.passkeys import stepup
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import (
    CLIENT_ID,
    CLIENT_SECRET,
    FakeGoogle,
    FakeSF,
    _identity_body,
    _resp,
    _token_body,
    endorse,
    sf_app,
    vault_key,
)
from tests.fixtures.vault_zk import conn_ref, ref

pytestmark = pytest.mark.django_db(transaction=True)

CLI_ROOT = Path(os.environ.get("CANOPY_CLI_ROOT") or Path(__file__).resolve().parents[2] / "canopy-connections-zk")
CLI_PYTHON = CLI_ROOT / ".venv" / "bin" / "python"

GOOGLE_CLIENT, GOOGLE_SECRET, GOOGLE_REFRESH = "goog-client", "goog-secret", "1//refresh"

# The CLI's real entry point, with the provider's HTTP call and every non-loopback socket
# replaced. Runs under the CLI's interpreter, so it imports nothing from canopy-web.
WRAPPER = textwrap.dedent('''
    import fcntl, json, os, socket, sys, time

    def _note(path, row):
        with open(path, "a") as fh:
            fh.write(json.dumps(row) + "\\n")

    _getaddrinfo, _connect = socket.getaddrinfo, socket.socket.connect
    LOCAL = ("127.0.0.1", "::1", "localhost")

    def guarded_getaddrinfo(host, *a, **k):
        if host not in LOCAL:
            _note(os.environ["E2E_BLOCKED_LOG"], {"host": str(host)})
            raise socket.gaierror(f"e2e: refused to resolve {host}")
        return _getaddrinfo(host, *a, **k)

    def guarded_connect(self, address):
        if isinstance(address, tuple) and address[0] not in LOCAL:
            _note(os.environ["E2E_BLOCKED_LOG"], {"host": str(address[0])})
            raise OSError(f"e2e: refused to connect to {address[0]}")
        return _connect(self, address)

    socket.getaddrinfo, socket.socket.connect = guarded_getaddrinfo, guarded_connect

    from orchestrator.connections.providers import base

    def fake_post_form(url, data, *, timeout):
        time.sleep(float(os.environ.get("E2E_PROVIDER_DELAY") or 0))  # a slow provider, to overlap two machines
        with open(os.environ["E2E_PROVIDER_STATE"] + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(os.environ["E2E_PROVIDER_STATE"]) as fh:
                state = json.load(fh)
            status, body = answer(state, url, dict(data))
            _note(os.environ["E2E_PROVIDER_LOG"], {"url": url, "form": dict(data), "status": status,
                                                   "machine": os.environ.get("E2E_MACHINE", "")})
            with open(os.environ["E2E_PROVIDER_STATE"], "w") as fh:
                json.dump(state, fh)
        return base.HttpResponse(status, body)

    def answer(state, url, form):
        apps = state["apps"]
        if url == "https://login.salesforce.com/services/oauth2/token":
            app, token = apps["salesforce"], form.get("refresh_token")
            if (form.get("client_id"), form.get("client_secret")) != (app["id"], app["secret"]):
                return 400, {"error": "invalid_client"}
            if token not in state["live"]:
                return 400, {"error": "invalid_grant", "error_description": "expired access/refresh token"}
            state["live"].remove(token)
            state["n"] += 1
            new = f"5Aep_rotated_{state['n']}"
            state["live"].append(new)
            return 200, {"access_token": f"00D!access_{state['n']}", "refresh_token": new,
                         "instance_url": "https://dimagi.my.salesforce.com", "token_type": "Bearer",
                         "scope": "api refresh_token"}
        if url == "https://oauth2.googleapis.com/token":
            app, token = apps["google"], form.get("refresh_token")
            if (form.get("client_id"), form.get("client_secret")) != (app["id"], app["secret"]):
                return 400, {"error": "invalid_client"}
            if token not in state["live"]:
                return 400, {"error": "invalid_grant"}
            state["n"] += 1
            return 200, {"access_token": f"ya29.access_{state['n']}", "expires_in": 3599, "token_type": "Bearer",
                         "scope": "openid email"}
        if url in ("https://login.salesforce.com/services/oauth2/revoke", "https://oauth2.googleapis.com/revoke"):
            if form.get("token") in state["live"]:
                state["live"].remove(form["token"])
                state["revoked"].append(form["token"])
                return 200, {}
            return 400, {"error": "invalid_token"}
        return 400, {"error": "unexpected_url", "error_description": url}

    base.post_form = fake_post_form

    if os.environ.get("E2E_MODE") == "probe":
        # One raw call as this machine, read the way the CLI reads an answer.
        from orchestrator import canopy_web, device_key
        from orchestrator.cred_cli import _error_code, _machine_call, _problem, _status_of
        job = json.load(sys.stdin)
        call = _machine_call(job["token"])
        if job.get("no_proof"):
            call = lambda m, p, b=None: canopy_web.call(m, p, b, token=job["token"])
        if job.get("wrong_htu"):
            def call(m, p, b=None):
                url = canopy_web.resolve_base_url(None) + "/api/harness/runners/elsewhere"
                return canopy_web.call(m, p, b, token=job["token"], headers=device_key.proof_headers(m, url))
        try:
            print(json.dumps({"ok": True, "body": call(job["method"], job["path"], job.get("body"))}))
        except canopy_web.CanopyError as e:
            print(json.dumps({"ok": False, "status": _status_of(e), "code": _error_code(e),
                              "problem": _problem(e), "raw": str(e)[:600]}))
        sys.exit(0)

    from orchestrator.cli import main
    sys.argv[0] = "canopy"
    main()
''')


# ---- recording the story ------------------------------------------------------------------

class Story:
    """Each scenario's checks, without stopping at the first that fails."""

    def __init__(self):
        self.rows: list[dict] = []
        self.row: dict = {}

    @contextmanager
    def step(self, name: str):
        self.row = {"name": name, "checks": [], "error": None}
        self.rows.append(self.row)
        try:
            yield
        except Exception:  # noqa: BLE001 - a broken scenario is a finding, the next still runs
            self.row["error"] = traceback.format_exc(limit=8)

    def check(self, what: str, ok, detail="") -> bool:
        self.row["checks"].append({"what": what, "ok": bool(ok), "detail": "" if ok else str(detail)[:1800]})
        return bool(ok)

    def failed(self) -> list[dict]:
        return [r for r in self.rows if r["error"] or any(not c["ok"] for c in r["checks"])]

    def render(self) -> str:
        out = []
        for r in self.rows:
            bad = r["error"] or any(not c["ok"] for c in r["checks"])
            out.append(f"{'FAIL' if bad else 'pass'}  {r['name']}")
            for c in r["checks"]:
                if not c["ok"]:
                    out.append(f"      x {c['what']}\n        {c['detail']}")
            if r["error"]:
                out.append(textwrap.indent(r["error"], "      "))
        return "\n".join(out)


# ---- a machine: a home directory and the real CLI ---------------------------------------

class Run:
    def __init__(self, p: subprocess.CompletedProcess):
        self.rc, self.out, self.err = p.returncode, p.stdout, p.stderr

    def __str__(self):
        return f"exit {self.rc}\nstdout: {self.out.strip()[:700]}\nstderr: {self.err.strip()[-900:]}"


class Lab:
    """Where the run's files live, and the live server's address."""

    def __init__(self, root: Path, base_url: str):
        self.root, self.base_url = root, base_url
        self.wrapper = root / "cli_wrapper.py"
        self.wrapper.write_text(WRAPPER)
        self.state, self.log, self.blocked = root / "provider.json", root / "provider.log", root / "blocked.log"
        self.log.touch()
        self.blocked.touch()
        self.state.write_text(json.dumps({"n": 0, "live": [], "revoked": [], "apps": {
            "salesforce": {"id": CLIENT_ID, "secret": CLIENT_SECRET},
            "google": {"id": GOOGLE_CLIENT, "secret": GOOGLE_SECRET}}}))

    def provider(self) -> dict:
        return json.loads(self.state.read_text())

    def seed_refresh(self, token: str) -> None:
        state = self.provider()
        state["live"].append(token)
        self.state.write_text(json.dumps(state))

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def blocked_hosts(self) -> list[str]:
        return [json.loads(line)["host"] for line in self.blocked.read_text().splitlines()]


class Machine:
    """One box or computer. `runner`/`key` are its canopy-web rows, when it has them."""

    def __init__(self, lab: Lab, name: str, runner: Runner | None = None):
        self.lab, self.name, self.runner = lab, name, runner
        self.key: RunnerDeviceKey | None = None
        self.home = lab.root / "machines" / name
        self.home.mkdir(parents=True)

    def env(self, **extra) -> dict:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "LANG": "C.UTF-8",
               "CANOPY_DEVICE_KEY_DIR": str(self.home / "device-key"), "CANOPY_DEVICE_KEY_KIND": "software",
               "CANOPY_WEB_API_URL": self.lab.base_url, "E2E_MACHINE": self.name,
               "E2E_PROVIDER_STATE": str(self.lab.state), "E2E_PROVIDER_LOG": str(self.lab.log),
               "E2E_BLOCKED_LOG": str(self.lab.blocked)}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def cli(self, *args, stdin: str | None = None, **env) -> Run:
        return Run(subprocess.run([str(CLI_PYTHON), str(self.lab.wrapper), *args], env=self.env(**env),
                                  cwd=self.home, capture_output=True, text=True, input=stdin, timeout=200))

    def start(self, *args, **env) -> subprocess.Popen:
        return subprocess.Popen([str(CLI_PYTHON), str(self.lab.wrapper), *args], env=self.env(**env), cwd=self.home,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def enroll(self, pat: str, approve_by=None) -> RunnerDeviceKey:
        """`canopy vault device --enroll` through the real route; then what a person's
        passkey approval leaves behind, when `approve_by` is given."""
        run = self.cli("vault", "device", "--enroll", "--software", "--runner", str(self.runner.pk), "--json",
                       CANOPY_WEB_PAT=pat)
        assert run.rc == 0, f"enroll {self.name}: {run}"
        key = RunnerDeviceKey.objects.get(runner=self.runner)
        if approve_by is not None:
            now = timezone.now()
            key.status, key.approved_by, key.approved_at, key.ecdh_approved_at = (
                RunnerDeviceKey.ACTIVE, approve_by, now, now)
            key.save()
        self.key = key
        return key

    def runner_token(self) -> str:
        return RunnerAccessToken.mint(self.key)[0]

    def stage(self, turn: Turn, *flags, provider="salesforce", agent="eva", token: str | None = None, **env) -> Run:
        """As `cloud_runner._stage_connection_token` runs it: the bearer in $CANOPY_TOKEN."""
        return self.cli(*self.stage_args(turn, *flags, provider=provider, agent=agent),
                        CANOPY_TOKEN=token or self.runner_token(), **env)

    def stage_args(self, turn: Turn, *flags, provider="salesforce", agent="eva") -> tuple:
        return ("cred", "connection-token", provider, "--runner", "--agent", agent,
                "--runner-id", str(self.runner.pk), "--turn", str(turn.pk), *flags)

    def keep(self) -> Run:
        return self.cli("vault", "zk", "keep")

    def probe(self, method, path, body=None, *, token=None, **flags) -> dict:
        job = {"method": method, "path": path, "body": body, "token": token or self.runner_token(), **flags}
        run = self.cli(stdin=json.dumps(job), E2E_MODE="probe")
        assert run.rc == 0, f"probe: {run}"
        return json.loads(run.out)

    def staged(self, slug="eva", provider="salesforce") -> Path:
        return self.home / ".canopy" / "connections" / slug / f"{provider}.json"

    def pins(self) -> dict:
        path = self.home / ".canopy" / "connection-pins.json"
        return json.loads(path.read_text()) if path.exists() else {}


# ---- the owner's browser ------------------------------------------------------------------

class Browser:
    """The owner at a browser: a real session cookie and CSRF token over real HTTP; the
    crypto `frontend/src/vault` does, done by the normative reference."""

    def __init__(self, base_url: str, user, priv: ec.EllipticCurvePrivateKey | None, *, stepped_up=True):
        self.base_url, self.user, self.priv = base_url, user, priv
        c = Client()
        c.force_login(user)
        session = c.session
        # What a passkey touch leaves behind (`stepup.mark_stepped_up`).
        if stepped_up:
            session[stepup.SESSION_KEY] = {"user": user.pk,
                                           "until": (timezone.now() + timedelta(minutes=30)).timestamp()}
        session.save()
        self.session_id = session.session_key
        self.csrf = self.req("GET", "/api/csrf/").cookies.get(django_settings.CSRF_COOKIE_NAME, "")

    def req(self, method, path, body=None, *, form=None, csrf=True):
        cookies = f"{django_settings.SESSION_COOKIE_NAME}={self.session_id}"
        headers = {}
        if csrf and getattr(self, "csrf", ""):
            cookies += f"; {django_settings.CSRF_COOKIE_NAME}={self.csrf}"
            headers["X-CSRFToken"] = self.csrf
        headers["Cookie"] = cookies
        return requests.request(method, self.base_url + path, json=body, data=form, headers=headers,
                                allow_redirects=False, timeout=30)

    def connect(self, provider: str, workspace: str, fake, *, agent: str = "") -> str:
        """The two legs through the real views, the provider's own page skipped: the
        connection's id, from where the callback lands (the share screen)."""
        start = f"/auth/connections/{provider}/{workspace}/start/"
        r = self.req("POST", start, form={"agent": agent}) if agent else self.req("GET", start)
        assert r.status_code == 302, f"start: {r.status_code} {r.text[:300]}"
        where = urllib.parse.urlsplit(r.headers["Location"])
        assert where.scheme == "https", f"start landed on {r.headers['Location']}"
        state = dict(urllib.parse.parse_qsl(where.query))["state"]
        with fake:
            r = self.req("GET", f"/auth/connections/{provider}/{workspace}/callback/?"
                         + urllib.parse.urlencode({"code": "the-code", "state": state}))
        assert r.status_code == 302, f"callback: {r.status_code} {r.text[:300]}"
        found = re.match(r"/w/[^/]+/connections/([0-9a-f-]{36})/share", r.headers["Location"])
        assert found, f"callback landed on {r.headers['Location']}"
        return found.group(1)

    def share_state(self, conn_id) -> dict:
        r = self.req("GET", f"/api/connections/{conn_id}/share")
        assert r.status_code == 200, f"share state: {r.status_code} {r.text[:400]}"
        return r.json()

    def open_refresh(self, state: dict) -> dict:
        return json.loads(ref.open_sealed_value(state["sealed"], state["binding"], "refresh", "member",
                                                str(self.user.pk), self.priv))

    def share(self, conn_id, *, agents=(), my_computers=(), endorsers=()):
        """Open what is stored, sign a statement and seal to who it allows, trusting a
        machine as `vault/trust.ts` does: the owner's own choice, or an endorsement it
        verifies itself."""
        s = self.share_state(conn_id)
        plain = self.open_refresh(s)
        version = (s["statement"] or {}).get("version", 0) + 1
        agent_ids = sorted(str(a.pk) for a in agents)
        statement = {"account_id": s["account_id"], "agents": agent_ids, "connection": s["id"],
                     "endorsers": sorted(endorsers), "my_computers": sorted(my_computers), "v": 1, "version": version}
        me = ref.jkt(self.priv.public_key())
        trusted = {me, *endorsers}
        vouched = set()
        for e in s["endorsements"]:
            jwk = e.get("by_public_jwk")
            if jwk and e["kind"] == "device" and e["target"] == e["key_jkt"] and e.get("agent_id") in agent_ids:
                pub = _public(jwk)
                if ref.jkt(pub) in trusted and ref.verify_endorsement(e, e["agent_id"], pub):
                    vouched.add(e["target"])
        recipients = [("member", str(self.user.pk), self.priv.public_key())]
        seen = set()
        for c in s["candidates"]:
            if c["kind"] == "device" and c["target"] not in seen and (
                    c["target"] in my_computers or c["target"] in vouched):
                seen.add(c["target"])
                recipients.append(("device", c["target"], _public(c["public_jwk"])))
        plain["statement_floor"] = version
        sealed = _seal(conn_ref.compact(plain), s["binding"], "refresh", recipients)
        pubs = {(k, t): q for k, t, q in recipients}
        sig = conn_ref.writer_sig(s["binding"], "refresh", s["sealed_digest"], sealed, self.priv, "member",
                                  str(self.user.pk), pubs)
        return self.req("PUT", f"/api/connections/{conn_id}/share", {
            "statement": statement, "sig": conn_ref.sign_statement(statement, self.priv), "by_jkt": me,
            "sealed": sealed, "sealed_sig": sig, "expect": s["sealed_digest"]})


def _public(jwk: dict) -> ec.EllipticCurvePublicKey:
    return ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b"\x04" + ref.unb64u(jwk["x"]) + ref.unb64u(jwk["y"]))


def _seal(plaintext: str, binding: str, name: str, recipients: list) -> str:
    ephemerals = [ec.generate_private_key(ec.SECP256R1()) for _ in recipients]
    nonces = [os.urandom(12) for _ in recipients]
    return ref.seal_value(plaintext, binding, name, 0, recipients, ephemerals, nonces)


# ---- the world ----------------------------------------------------------------------------

class World:
    """jj (with a vault key) owns eva, which op also administers. op runs eva's cloud boxes.
    jj has a computer of their own. Machines exist only once a scenario makes them."""

    def __init__(self, lab: Lab):
        users = get_user_model().objects
        self.lab = lab
        self.jj = users.create_user(username="jj", email="jj@dimagi.com")
        self.jj_priv = vault_key(self.jj)
        self.op = users.create_user(username="op", email="op@dimagi.com")
        self.ws = Workspace.objects.create(slug="w", display_name="W", created_by=self.jj)
        for u in (self.jj, self.op):
            WorkspaceMembership.objects.create(workspace=self.ws, user=u, role=WorkspaceMembership.EDITOR)
        self.app = sf_app(self.ws)
        self.eva = Agent.objects.create(slug="eva", name="Eva", workspace=self.ws, owner=self.jj)
        AgentAdmin.objects.create(agent=self.eva, user=self.op)
        self.jj_pat = PersonalToken.create_for_user(user=self.jj, label="cli")[0]
        self.op_pat = PersonalToken.create_for_user(user=self.op, label="cli")[0]

    def box(self, name: str, *, endorsed=True) -> Machine:
        """A cloud box running eva, op's, its key enrolled by the CLI and approved."""
        runner = Runner.objects.create(name=name, kind=Runner.CLOUD, owner=self.op, workspace=self.ws,
                                       last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
        RunnerAssignment.objects.create(agent=self.eva, runner=runner, rank=0)
        m = Machine(self.lab, name, runner)
        key = m.enroll(self.op_pat, approve_by=self.op)
        if endorsed:
            endorse(self.eva, key, self.jj, self.jj_priv)
        return m

    def computer(self, name: str, owner, pat: str, *, approved=True) -> Machine:
        runner = Runner.objects.create(name=name, kind=Runner.COMPUTER, owner=owner, workspace=self.ws,
                                       last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
        m = Machine(self.lab, name, runner)
        m.enroll(pat, approve_by=owner if approved else None)
        return m

    def turn(self, machine: Machine, agent=None) -> Turn:
        return Turn.objects.create(agent=agent or self.eva, origin=Turn.ORIGIN_EMAIL, prompt="x",
                                   idempotency_key=uuid.uuid4().hex, status=Turn.CLAIMED, claimed_by=machine.runner)


def _row_text(conn_id) -> str:
    return json.dumps(model_to_dict(Connection.objects.get(pk=conn_id)), default=str)


def _no_secret_at_rest(story: Story, conn_id, secrets: list[str]) -> None:
    row = Connection.objects.get(pk=conn_id)
    text, events = _row_text(conn_id), json.dumps(list(Event.objects.values()), default=str)
    story.check("refresh envelope is ciphertext", row.sealed.startswith("zks1:"), row.sealed[:40])
    for s in secrets:
        story.check(f"the row does not hold {s!r}", s not in text)
        story.check(f"no event holds {s!r}", s not in events)


def _staged_ok(story: Story, machine: Machine, run: Run, token: str, *, slug="eva", provider="salesforce",
               instance="https://dimagi.my.salesforce.com") -> None:
    path = machine.staged(slug, provider)
    story.check("exit 0", run.rc == 0, run)
    story.check("prints the staged path", run.out.strip() == str(path), run)
    story.check("the token is not printed", token not in run.out and token not in run.err, run)
    if not path.exists():
        story.check("the file exists", False, path)
        return
    body = json.loads(path.read_text())
    story.check("file fields", set(body) == {"access_token", "instance_url", "expires_at", "account_label"}, body)
    story.check(f"file holds {token}", body.get("access_token") == token, body.get("access_token"))
    story.check("file instance_url", body.get("instance_url") == instance, body.get("instance_url"))
    story.check("file expires in the future", float(body.get("expires_at") or 0) > timezone.now().timestamp())
    story.check("file mode 0600", stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode))
    story.check("dir mode 0700", stat.S_IMODE(path.parent.stat().st_mode) == 0o700, oct(path.parent.stat().st_mode))


# ---- the story ----------------------------------------------------------------------------

@pytest.mark.skipif(not CLI_PYTHON.exists(), reason=f"no canopy CLI checkout at {CLI_ROOT}")
@pytest.mark.timeout(1200)
def test_the_real_cli_against_the_real_server(live_server, tmp_path, settings, caplog):
    # The real gates: the test settings turn sign-in off by default.
    settings.REQUIRE_AUTH = True
    lab = Lab(tmp_path, live_server.url)
    story = Story()
    world = World(lab)
    browser = Browser(live_server.url, world.jj, world.jj_priv)
    box1 = box2 = None
    conn_id = None
    first_refresh = "5Aep_refresh_1"

    with story.step("0 machines enrol through the real route; the bearer and proof are accepted"):
        box1 = world.box("eva-cloud-1")
        story.check("box 1's key is approved", box1.key.status == RunnerDeviceKey.ACTIVE)
        story.check("the CLI registered an ECDH key", bool(box1.key.ecdh_jkt))

    with story.step("1 connect: the server's first seal, unshared, a machine gets not_connected"):
        fake = FakeSF(token=[_resp(body=_token_body(refresh_token=first_refresh))],
                      identity=_resp(body=_identity_body()))
        conn_id = browser.connect("salesforce", "w", fake)
        lab.seed_refresh(first_refresh)
        conn = Connection.objects.get(pk=conn_id)
        story.check("state is unshared", conn.state == Connection.UNSHARED, conn.state)
        story.check("sealed_sig is null (R4)", conn.sealed_sig is None, conn.sealed_sig)
        story.check("access_sealed is empty (R5)", conn.access_sealed == "")
        _no_secret_at_rest(story, conn_id, [first_refresh, "00D!access_1"])
        state = browser.share_state(conn_id)
        plain = browser.open_refresh(state)
        story.check("the first seal has exactly the contract's fields", set(plain) == {
            "v", "provider", "refresh_token", "client_id", "login_host", "instance_url", "account_id", "owner_key",
            "issued_at", "scopes", "statement_floor"}, sorted(plain))
        story.check("the first seal holds the refresh token", plain.get("refresh_token") == first_refresh)
        story.check("its floor is 0", plain.get("statement_floor") == 0, plain.get("statement_floor"))
        story.check("its owner key is the owner's", plain.get("owner_key") == ref.jkt(world.jj_priv.public_key()))
        turn = world.turn(box1)
        run = box1.stage(turn)
        story.check("--runner exits 3 (not_connected)", run.rc == 3, run)
        story.check("nothing staged", not box1.staged().exists())
        p = box1.probe("GET", f"/api/harness/runners/{box1.runner.pk}/turns/{turn.pk}/connections/salesforce")
        story.check("the body parses to not_connected", (p.get("status"), p.get("code")) == (409, "not_connected"), p)

    with story.step("2 the owner shares with an agent through the real PUT share route"):
        path = f"/api/connections/{conn_id}/share"
        unsure = Browser(live_server.url, world.jj, world.jj_priv, stepped_up=False)
        # Well formed, so it reaches the route: Ninja answers 422 on a bad body before any view runs.
        shaped = {"statement": {}, "sig": "x", "by_jkt": "x", "sealed": "x", "sealed_sig": None, "expect": "x"}
        r = unsure.req("PUT", path, shaped)
        story.check("without a passkey touch: 403 step_up_required", r.status_code == 403
                    and r.json().get("error") == "step_up_required", f"{r.status_code} {r.text[:300]}")
        r = browser.req("PUT", path, shaped, csrf=False)
        story.check("without a CSRF token: refused", r.status_code == 403, f"{r.status_code} {r.text[:300]}")
        r = requests.put(live_server.url + path, json=shaped, timeout=30)
        story.check("without a session: refused", r.status_code in (401, 403), f"{r.status_code} {r.text[:300]}")
        r = Browser(live_server.url, world.op, None).req("GET", path)
        story.check("someone else: 404", r.status_code == 404, f"{r.status_code} {r.text[:300]}")
        r = browser.share(conn_id, agents=[world.eva])
        story.check("PUT share answers 200", r.status_code == 200, f"{r.status_code} {r.text[:800]}")
        conn = Connection.objects.get(pk=conn_id)
        story.check("state is active", conn.state == Connection.ACTIVE, conn.state)
        story.check("statement version 1", conn.statement_version == 1, conn.statement_version)
        story.check("a grant follows the statement", conn.grants.filter(agent=world.eva).exists())
        story.check("sealed to the box", ("device", box1.key.ecdh_jkt) in {
            (e["kind"], e["target"]) for e in conn_ref.sealed_entries(conn.sealed)},
            conn_ref.sealed_entries(conn.sealed))

    with story.step("3 stage on a box: lease, read, rotating refresh, write-back, release"):
        before = len(lab.calls())
        turn = world.turn(box1)
        run = box1.stage(turn)
        calls = lab.calls()[before:]
        story.check("one provider call", len(calls) == 1, calls)
        if calls:
            story.check("to the sealed host", calls[0]["url"] == "https://login.salesforce.com/services/oauth2/token",
                        calls[0]["url"])
            story.check("with the sealed refresh token", calls[0]["form"].get("refresh_token") == first_refresh,
                        calls[0]["form"].get("refresh_token"))
            story.check("the provider took it", calls[0]["status"] == 200, calls[0])
        _staged_ok(story, box1, run, "00D!access_1")
        conn = Connection.objects.get(pk=conn_id)
        story.check("the lease is released", conn.lease_id is None, conn.lease_id)
        story.check("access_sealed is ciphertext", conn.access_sealed.startswith("zks1:"), conn.access_sealed[:30])
        story.check("access_sig is by the box",
                    (conn.access_sig or {}).get("by", {}).get("target") == box1.key.ecdh_jkt)
        story.check("the rotated refresh envelope was written, by the box",
                    (conn.sealed_sig or {}).get("by", {}).get("target") == box1.key.ecdh_jkt, conn.sealed_sig)
        story.check("access_expires_at stored", conn.access_expires_at is not None)
        _no_secret_at_rest(story, conn_id, [first_refresh, "5Aep_rotated_1", "00D!access_1"])
        story.check("the box pinned the statement", box1.pins().get(conn_id, {}).get("version") == 1, box1.pins())
        run = box1.cli("cred", "connection-token", "salesforce", "--agent", "eva")
        story.check("the turn's half: --agent reads the staged path, no network", run.rc == 0
                    and run.out.strip() == str(box1.staged()), run)

    with story.step("4 a second stage on the same box reuses the access token, no provider call"):
        before = len(lab.calls())
        run = box1.stage(world.turn(box1), "--min-life", "900")  # cloud_runner's CONNECTION_TOKEN_MIN_LIFE
        story.check("no provider call", len(lab.calls()) == before, lab.calls()[before:])
        _staged_ok(story, box1, run, "00D!access_1")

    with story.step("5 a second box: waiting (exit 4), the keeper on box 1 re-seals, box 2 stages"):
        box2 = world.box("eva-cloud-2")
        turn2 = world.turn(box2)
        run = box2.stage(turn2)
        story.check("exit 4 (waiting)", run.rc == 4, run)
        story.check("box 2 staged nothing", not box2.staged().exists())
        before_keep = Connection.objects.get(pk=conn_id).sealed_digest
        listed = browser.req("GET", "/api/connections/")
        rows = [c for c in listed.json().get("connections", []) if c.get("id") == conn_id] if listed.ok else []
        story.check("Settings lists it, with box 2 waiting", bool(rows) and rows[0].get("waiting") == [
            {"agent": "eva", "runner": "eva-cloud-2"}], f"{listed.status_code} {rows or listed.text[:400]}")
        story.check("and box 1 holding", bool(rows) and "eva-cloud-1" in rows[0].get("holders", []), rows)
        keep = box1.keep()
        story.check("keeper exits 0", keep.rc == 0, keep)
        story.check("keeper names box 2", "eva-cloud-2" in keep.out, keep)
        conn = Connection.objects.get(pk=conn_id)
        story.check("the row changed", conn.sealed_digest != before_keep, keep)
        story.check("sealed to box 2 too", ("device", box2.key.ecdh_jkt) in {
            (e["kind"], e["target"]) for e in conn_ref.sealed_entries(conn.sealed)}, keep)
        before = len(lab.calls())
        run = box2.stage(turn2)
        story.check("box 2 needed no refresh: the keeper re-sealed access too", len(lab.calls()) == before,
                    lab.calls()[before:])
        _staged_ok(story, box2, run, "00D!access_1")

    with story.step("6 rotation again from box 2; box 1 reads the rotated envelope and works"):
        before = len(lab.calls())
        run = box2.stage(world.turn(box2), "--refresh")
        calls = lab.calls()[before:]
        story.check("box 2 refreshed once", len(calls) == 1, calls)
        if calls:
            story.check("with the token box 1 rotated to", calls[0]["form"].get("refresh_token") == "5Aep_rotated_1",
                        calls[0]["form"].get("refresh_token"))
        _staged_ok(story, box2, run, "00D!access_2")
        conn = Connection.objects.get(pk=conn_id)
        story.check("box 2 wrote the refresh envelope", (conn.sealed_sig or {}).get("by", {}).get("target")
                    == box2.key.ecdh_jkt)
        before = len(lab.calls())
        run = box1.stage(world.turn(box1))
        story.check("box 1 reads box 2's access with no provider call", len(lab.calls()) == before,
                    lab.calls()[before:])
        _staged_ok(story, box1, run, "00D!access_2")
        before = len(lab.calls())
        run = box1.stage(world.turn(box1), "--refresh")
        calls = lab.calls()[before:]
        story.check("box 1 refreshes with box 2's rotated token", bool(calls)
                    and calls[0]["form"].get("refresh_token") == "5Aep_rotated_2", calls)
        _staged_ok(story, box1, run, "00D!access_3")

    with story.step("F real-CLI facts: gates, DPoP htu, error bodies parse to the codes the CLI expects"):
        material = f"/api/harness/runners/{box1.runner.pk}/turns/{world.turn(box1).pk}/connections/salesforce"
        ok = box1.probe("GET", material)
        story.check("a runner token and a device proof pass (the htu matches through the real URL)", ok.get("ok"), ok)
        p = box1.probe("GET", material, no_proof=True)
        story.check("no proof: 401 device_proof_required",
                    (p.get("status"), p.get("code")) == (401, "device_proof_required"), p)
        p = box1.probe("GET", material, wrong_htu=True)
        story.check("a proof for another URL: 401 device_proof_required", (p.get("status"), p.get("code"))
                    == (401, "device_proof_required"), p)
        p = box2.probe("GET", material, token=box1.runner_token())
        story.check("box 2's proof with box 1's token: 401 device_proof_required", (p.get("status"), p.get("code"))
                    == (401, "device_proof_required"), p)
        lease_path = f"/api/harness/runners/{box1.runner.pk}/connections/{conn_id}/refresh-lease"
        taken = box1.probe("POST", lease_path, {})
        story.check("a lease is taken", taken.get("ok") and taken["body"].get("ttl_seconds") == 60, taken)
        p = box2.probe("POST", f"/api/harness/runners/{box2.runner.pk}/connections/{conn_id}/refresh-lease", {})
        story.check("another box: 409 lease_held with retry_after",
                    (p.get("status"), p.get("code")) == (409, "lease_held")
                    and p.get("problem", {}).get("retry_after"), p)
        sealed_path = f"/api/harness/runners/{box2.runner.pk}/connections/{conn_id}/sealed"
        now = timezone.now().isoformat()
        p = box2.probe("PUT", sealed_path, {"lease_id": taken["body"]["lease_id"], "access_sealed": "zks1:x",
                                            "access_sig": None, "access_expires_at": now, "expect": "0" * 64,
                                            "rotated": False})
        story.check("a write-back under another's lease: 409 lease_lost", (p.get("status"), p.get("code"))
                    == (409, "lease_lost"), p)
        p = box1.probe("PUT", f"/api/harness/runners/{box1.runner.pk}/connections/{conn_id}/sealed", {
            "lease_id": taken["body"]["lease_id"], "access_sealed": "zks1:x", "access_sig": None,
            "access_expires_at": now, "expect": "0" * 64, "rotated": False})
        story.check("a stale expect: 409 zk_value_changed",
                    (p.get("status"), p.get("code")) == (409, "zk_value_changed"), p)
        box1.probe("DELETE", f"{lease_path}/{taken['body']['lease_id']}")
        story.check("the lease is given back", Connection.objects.get(pk=conn_id).lease_id is None)
        # With no CANOPY_TOKEN the CLI signs in itself (`runner_session`, POST /api/runner-sessions/).
        run = box1.cli("cred", "connection-token", "salesforce", "--runner", "--agent", "eva",
                       "--runner-id", str(box1.runner.pk), "--turn", str(world.turn(box1).pk))
        story.check("the box's own runner session passes the same gates", run.rc == 0, run)
        foreign = f"/api/harness/runners/{box2.runner.pk}/turns/{world.turn(box2).pk}/connections/salesforce"
        p = box1.probe("GET", foreign)
        story.check("box 1's token and proof on box 2's route: refused",
                    not p.get("ok") and p.get("status") in (401, 403, 404), p)
        ended = world.turn(box1)
        Turn.objects.filter(pk=ended.pk).update(status=Turn.DONE)
        run = box1.stage(ended)
        story.check("a turn that has ended: exit 3", run.rc == 3, run)
        run = box1.cli("cred", "salesforce-token", "--runner", "--agent", "eva", "--runner-id", str(box1.runner.pk),
                       "--turn", str(world.turn(box1).pk), CANOPY_TOKEN=box1.runner_token())
        story.check("the salesforce-token alias works", run.rc == 0, run)
        r = browser.req("GET", "/api/me/connections/salesforce/material")
        story.check("a browser session on the machines' route: 403 token_route_needs_pat", r.status_code == 403
                    and r.json().get("error") == "token_route_needs_pat", f"{r.status_code} {r.text[:300]}")

    with story.step("B the runner's environment: CANOPY_BASE_URL alone reaches this canopy-web"):
        # `cloud_runner._stage_connection_token` sets CANOPY_BASE_URL and not CANOPY_WEB_API_URL.
        blocked = len(lab.blocked_hosts())
        run = box1.stage(world.turn(box1), CANOPY_BASE_URL=lab.base_url, CANOPY_WEB_API_URL=None)
        hosts = lab.blocked_hosts()[blocked:]
        story.check("the CLI goes to CANOPY_BASE_URL, not its default", not hosts, f"tried {hosts}; {run}")
        story.check("and stages", run.rc == 0, run)

    after_base_url_check = len(lab.blocked_hosts())

    with story.step("S the owner shares again: version 2; a box follows the pin; a rollback is refused"):
        old = Connection.objects.get(pk=conn_id)
        old_statement, old_sig = old.statement, old.statement_sig
        r = browser.share(conn_id, agents=[world.eva])
        story.check("PUT share answers 200", r.status_code == 200, f"{r.status_code} {r.text[:800]}")
        conn = Connection.objects.get(pk=conn_id)
        story.check("version 2", conn.statement_version == 2, conn.statement_version)
        story.check("the access envelope from before the share stays", conn.access_sealed == old.access_sealed)
        before = len(lab.calls())
        run = box1.stage(world.turn(box1))
        story.check("a box still reads the older access", run.rc == 0 and len(lab.calls()) == before, run)
        story.check("its pin moved to 2", box1.pins().get(conn_id, {}).get("version") == 2, box1.pins())
        Connection.objects.filter(pk=conn_id).update(statement=old_statement, statement_sig=old_sig)
        run = box1.stage(world.turn(box1))
        story.check("the old signed statement replayed at a box that saw 2: refused", run.rc == 1
                    and "statement-rollback" in run.err, run)
        Connection.objects.filter(pk=conn_id).update(statement=conn.statement, statement_sig=conn.statement_sig)
        run = box1.stage(world.turn(box1), "--refresh")
        story.check("a refresh under version 2 writes back", run.rc == 0, run)

    with story.step("E two machines at once: one refresh, the other waits for its result"):
        # `_newer` compares whole-second expiries, and the last refresh was a moment ago: a
        # second apart, as two refreshes are outside a test.
        time.sleep(1.2)
        before = len(lab.calls())
        args = box1.stage_args(world.turn(box1), "--refresh")
        first = box1.start(*args, CANOPY_TOKEN=box1.runner_token(), E2E_PROVIDER_DELAY="4")
        time.sleep(1.5)  # box 1 now holds the lease, and is at the provider
        run = box2.stage(world.turn(box2), "--refresh")
        out, err = first.communicate(timeout=120)
        story.check("box 1 stages", first.returncode == 0, f"{first.returncode} {out} {err}")
        story.check("box 2 waits and stages", run.rc == 0, run)
        calls = lab.calls()[before:]
        story.check("one provider call between them", len(calls) == 1, calls)
        if run.rc == 0 and first.returncode == 0:
            story.check("the same token", json.loads(box1.staged().read_text())["access_token"]
                        == json.loads(box2.staged().read_text())["access_token"])

    with story.step("R the provider refuses a dead token: needs_reconnect; connect again; share again; it works"):
        # The refresh token dies at the provider (revoked there, say).
        state = lab.provider()
        dead = state["live"].pop()
        lab.state.write_text(json.dumps(state))
        run = box1.stage(world.turn(box1), "--refresh")
        story.check("exit 3, its owner must connect it again", run.rc == 3 and "connect it again" in run.err, run)
        story.check("the row is needs_reconnect", Connection.objects.get(pk=conn_id).needs_reconnect)
        run = box2.stage(world.turn(box2))
        story.check("another box is refused, not silent (exit 1)", run.rc == 1 and "connect it again" in run.err, run)
        fake = FakeSF(token=[_resp(body=_token_body(refresh_token="5Aep_again"))],
                      identity=_resp(body=_identity_body()))
        again = browser.connect("salesforce", "w", fake)
        lab.seed_refresh("5Aep_again")
        story.check("the same row (R6)", again == conn_id, again)
        conn = Connection.objects.get(pk=conn_id)
        story.check("unshared again, no access, no failure",
                    conn.state == Connection.UNSHARED and not conn.access_sealed
                    and not conn.needs_reconnect, (conn.state, conn.access_sealed[:20], conn.needs_reconnect))
        story.check("its grant is kept", conn.grants.filter(agent=world.eva).exists())
        run = box1.stage(world.turn(box1))
        story.check("until it is shared: exit 3", run.rc == 3, run)
        r = browser.share(conn_id, agents=[world.eva])
        story.check("sharing again answers 200", r.status_code == 200, f"{r.status_code} {r.text[:800]}")
        story.check("at the next version", Connection.objects.get(pk=conn_id).statement_version == 3,
                    Connection.objects.get(pk=conn_id).statement_version)
        before = len(lab.calls())
        run = box1.stage(world.turn(box1))
        story.check("a box stages right after, with no lease left over from the refused refresh", run.rc == 0, run)
        if run.rc != 0:
            # WORKAROUND, recorded as a bug: wait out the lease `refreshed` never gave up.
            Connection.objects.filter(pk=conn_id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
            run = box1.stage(world.turn(box1))
            story.check("once that lease has lapsed, it stages", run.rc == 0, run)
            story.check("and taking it over raises no false alarm: that holder wrote nothing because the "
                        "provider refused, it did not die", "lease_expired_unwritten" not in caplog.text,
                        [line for line in caplog.text.splitlines() if "lease_expired_unwritten" in line][:1])
        calls = lab.calls()[before:]
        story.check("with the new refresh token", bool(calls) and calls[0]["form"].get("refresh_token") == "5Aep_again",
                    calls)
        story.check("not the dead one", dead not in [c["form"].get("refresh_token") for c in calls], calls)

    with story.step("V a box that is no longer vouched for loses it"):
        gone = AgentVaultEndorsement.objects.filter(agent=world.eva, kind="device", target=box2.key.ecdh_jkt)
        kept = list(gone.values())
        gone.delete()
        run = box2.stage(world.turn(box2))
        story.check("exit 3 (not_connected)", run.rc == 3, run)
        keep = box1.keep()
        story.check("the keeper does not hand it back", keep.rc == 0 and "eva-cloud-2" not in keep.out, keep)
        for row in kept:
            AgentVaultEndorsement.objects.create(**row)
        run = box2.stage(world.turn(box2))
        story.check("vouched for again, it works", run.rc in (0, 4), run)
        if run.rc == 4:
            box1.keep()
            run = box2.stage(world.turn(box2))
            story.check("after a keeper pass", run.rc == 0, run)

    with story.step("7 a forged recipient in the server's list: the box refuses"):
        real_fields = web_tokens.material_fields
        attacker = ec.generate_private_key(ec.SECP256R1())
        attacker_jkt = ref.jkt(attacker.public_key())
        forged_row = {"kind": "device", "target": attacker_jkt, "public_jwk": ref.jwk(attacker.public_key()),
                      "runner_name": "attacker"}

        def lying(*a, **k):
            out = real_fields(*a, **k)
            return {**out, "recipients": [*out["recipients"], forged_row]}

        digest = Connection.objects.get(pk=conn_id).sealed_digest
        before = len(lab.calls())
        with mock.patch("apps.connections.tokens.material_fields", lying):
            run = box1.stage(world.turn(box1), "--refresh")
        story.check("a stage with a forged recipient fails", run.rc not in (0, 3, 4), run)
        story.check("it names zk_unendorsed_recipient", "zk_unendorsed_recipient" in run.err, run)
        story.check("no provider call before refusing", len(lab.calls()) == before, lab.calls()[before:])
        story.check("nothing written", Connection.objects.get(pk=conn_id).sealed_digest == digest)
        story.check("the lease is released", Connection.objects.get(pk=conn_id).lease_id is None)

        # The same forgery, with an endorsement signed by a person the statement never names.
        endorser = ec.generate_private_key(ec.SECP256R1())
        forged_e = ref.endorse(world.eva.pk, "device", attacker_jkt, attacker.public_key(), endorser, world.op.pk)

        def lying_with_endorsement(*a, **k):
            out = lying(*a, **k)
            return {**out, "endorsements": [*out["endorsements"], {
                "kind": "device", "target": attacker_jkt, "key_jkt": forged_e["key_jkt"], "by": str(world.op.pk),
                "by_email": "op@dimagi.com", "by_jkt": forged_e["by_jkt"],
                "by_public_jwk": ref.jwk(endorser.public_key()),
                "sig": forged_e["sig"], "agent_id": str(world.eva.pk)}]}

        with mock.patch("apps.connections.tokens.material_fields", lying_with_endorsement):
            run = box1.stage(world.turn(box1), "--refresh")
        story.check("a stranger's endorsement does not admit it", run.rc not in (0, 3, 4)
                    and "zk_unendorsed_recipient" in run.err, run)

        # The keeper: a forged machine waiting.
        real_waiting = web_recipients.waiting

        def waiting_too(*a, **k):
            return [*real_waiting(*a, **k), {"agent": "eva", "runner": "attacker", "target": attacker_jkt}]

        digest = Connection.objects.get(pk=conn_id).sealed_digest
        with (mock.patch("apps.connections.tokens.material_fields", lying),
              mock.patch("apps.connections.recipients.waiting", waiting_too)):
            keep = box1.keep()
        story.check("the keeper does not seal to it", Connection.objects.get(pk=conn_id).sealed_digest == digest, keep)
        story.check("the keeper says why", "zk_unendorsed_recipient" in keep.out, keep)

        # Edit the row: a statement signature that is not the owner's.
        conn = Connection.objects.get(pk=conn_id)
        good = conn.statement_sig
        Connection.objects.filter(pk=conn_id).update(
            statement_sig=conn_ref.sign_statement(conn.statement, attacker))
        run = box1.stage(world.turn(box1))
        story.check("a statement not signed by the owner is refused", run.rc == 1 and "bad-statement-sig" in run.err,
                    run)
        Connection.objects.filter(pk=conn_id).update(statement_sig=good)

    with story.step("8 disconnect: revoking; the keeper revokes and confirms; the row is gone; a stage exits 3"):
        token_now = lab.provider()["live"][-1] if lab.provider()["live"] else ""
        r = browser.req("DELETE", f"/api/connections/{conn_id}")
        story.check("DELETE answers 204", r.status_code == 204, f"{r.status_code} {r.text[:400]}")
        conn = Connection.objects.filter(pk=conn_id).first()
        story.check("the row is revoking", conn is not None and conn.state == Connection.REVOKING,
                    conn.state if conn else "gone")
        story.check("revoke_at_provider", conn is not None and conn.revoke_at_provider)
        run = box1.stage(world.turn(box1))
        story.check("a stage while revoking exits 3", run.rc == 3, run)
        before = len(lab.calls())
        keep = box1.keep()
        story.check("the keeper exits 0", keep.rc == 0, keep)
        calls = lab.calls()[before:]
        story.check("one provider revoke", len(calls) == 1, f"{calls}\n{keep}")
        if calls:
            story.check("at the sealed host",
                        calls[0]["url"] == "https://login.salesforce.com/services/oauth2/revoke", calls[0])
            story.check("of the current refresh token", calls[0]["form"].get("token") == token_now, calls[0])
        story.check("the provider says it is revoked", token_now in lab.provider()["revoked"], lab.provider())
        story.check("the row is gone", not Connection.objects.filter(pk=conn_id).exists(), keep)
        run = box1.stage(world.turn(box1))
        story.check("a stage now exits 3", run.rc == 3, run)
        keep = box2.keep()
        story.check("box 2's keeper finds nothing to do", keep.rc == 0, keep)

    with story.step("9 person path: an enrolled computer with Use on my computers, a PAT; and the refusals"):
        computer = world.computer("jj-laptop", world.jj, world.jj_pat)
        fake = FakeSF(token=[_resp(body=_token_body(refresh_token="5Aep_person"))],
                      identity=_resp(body=_identity_body(user_id="005person", username="jj-person@dimagi.com")))
        person_id = browser.connect("salesforce", "w", fake)
        lab.seed_refresh("5Aep_person")
        run = computer.cli("cred", "connection-token", "salesforce", CANOPY_WEB_PAT=world.jj_pat)
        story.check("before sharing: exit 2 with the sentence", run.rc == 2 and "not shared" in run.err, run)
        r = browser.share(person_id, my_computers=[computer.key.ecdh_jkt])
        story.check("share with my_computers answers 200", r.status_code == 200, f"{r.status_code} {r.text[:800]}")
        before = len(lab.calls())
        run = computer.cli("cred", "connection-token", "salesforce", CANOPY_WEB_PAT=world.jj_pat)
        path = computer.home / ".canopy" / "connections" / "salesforce.json"
        story.check("exit 0", run.rc == 0, run)
        story.check("the cache path is printed", run.out.strip() == str(path), run)
        calls = lab.calls()[before:]
        story.check("the laptop refreshed",
                    len(calls) == 1 and calls[0]["form"].get("refresh_token") == "5Aep_person", calls)
        if path.exists():
            body = json.loads(path.read_text())
            token = body.get("access_token") or "?"
            story.check("the token is not printed", token not in run.out and token not in run.err, run)
            story.check("mode 0600", stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode))
            story.check("fields", set(body) == {"access_token", "instance_url", "expires_at", "account_label"}, body)
        else:
            story.check("the cache file exists", False, path)
        row = Connection.objects.filter(pk=person_id).first()
        story.check("the laptop wrote back", row is not None and row.access_sealed.startswith("zks1:"))
        before = len(lab.calls())
        run = computer.cli("cred", "connection-token", "salesforce", CANOPY_WEB_PAT=world.jj_pat)
        story.check("again: from the cache, no provider call", run.rc == 0 and len(lab.calls()) == before, run)
        run = computer.cli("cred", "connection-token", "salesforce", "--refresh", CANOPY_WEB_PAT=world.jj_pat)
        story.check("--refresh rotates and writes back", run.rc == 0 and len(lab.calls()) == before + 1,
                    f"{run}\n{lab.calls()[before:]}")

        run = computer.cli("cred", "connection-token", "salesforce", "--workspace", "w", "--refresh",
                           CANOPY_WEB_PAT=world.jj_pat)
        story.check("--workspace caches to its own file",
                    run.rc == 0 and run.out.strip().endswith("salesforce@w.json"), run)

        # Refusals, each with its sentence and the command to run.
        pending = world.computer("jj-new-laptop", world.jj, world.jj_pat, approved=False)
        run = pending.cli("cred", "connection-token", "salesforce", CANOPY_WEB_PAT=world.jj_pat)
        story.check("not yet approved: exit 2, the enroll sentence", run.rc == 2
                    and "canopy vault device --enroll" in run.err, run)
        keyless = Machine(lab, "keyless")
        run = keyless.cli("cred", "connection-token", "salesforce", CANOPY_WEB_PAT=world.jj_pat)
        story.check("no key at all: exit 2, the enroll sentence", run.rc == 2
                    and "canopy vault device --enroll" in run.err, run)
        other = world.computer("jj-other", world.jj, world.jj_pat)
        run = other.cli("cred", "connection-token", "salesforce", CANOPY_WEB_PAT=world.jj_pat)
        story.check("enrolled but not listed: exit 2, the Use on my computers sentence", run.rc == 2
                    and "Use on my computers" in run.err, run)
        strangers = world.computer("op-laptop", world.op, world.op_pat)
        run = strangers.cli("cred", "connection-token", "salesforce", CANOPY_WEB_PAT=world.jj_pat)
        story.check("someone else's machine: exit 2, the enroll sentence", run.rc == 2
                    and "canopy vault device --enroll" in run.err, run)

    with story.step("M one connection on a laptop and a box: each reads what the other rotated"):
        r = browser.share(person_id, agents=[world.eva], my_computers=[computer.key.ecdh_jkt])
        story.check("share answers 200", r.status_code == 200, f"{r.status_code} {r.text[:800]}")
        run = box1.stage(world.turn(box1))
        story.check("the box stages the laptop's access", run.rc == 0, run)
        before = len(lab.calls())
        run = computer.cli("cred", "connection-token", "salesforce", "--refresh", CANOPY_WEB_PAT=world.jj_pat)
        story.check("the laptop rotates and writes back", run.rc == 0 and len(lab.calls()) == before + 1, run)
        row = Connection.objects.get(pk=person_id)
        story.check("still sealed to the box (R18: a laptop never drops a holder)", ("device", box1.key.ecdh_jkt) in {
            (e["kind"], e["target"]) for e in conn_ref.sealed_entries(row.sealed)})
        token = json.loads((computer.home / ".canopy" / "connections" / "salesforce.json").read_text())["access_token"]
        before = len(lab.calls())
        run = box1.stage(world.turn(box1))
        story.check("the box reads the laptop's envelope", run.rc == 0 and len(lab.calls()) == before, run)
        _staged_ok(story, box1, run, token)
        before = len(lab.calls())
        run = box1.stage(world.turn(box1), "--refresh")
        story.check("the box rotates in turn", run.rc == 0 and len(lab.calls()) == before + 1, run)
        token = json.loads(box1.staged().read_text())["access_token"]
        before = len(lab.calls())
        run = computer.cli("cred", "connection-token", "salesforce", "--refresh", CANOPY_WEB_PAT=world.jj_pat)
        calls = lab.calls()[before:]
        story.check("the laptop refreshes with the token the box rotated to", run.rc == 0 and len(calls) == 1
                    and calls[0]["status"] == 200, f"{run}\n{calls}")

    with story.step("10 Google, an agent's own connection: non-rotating"):
        sf_app(world.ws, provider="google", client_id=GOOGLE_CLIENT, secret=GOOGLE_SECRET)
        google_id = browser.connect("google", "w", FakeGoogle(), agent="eva")
        lab.seed_refresh(GOOGLE_REFRESH)
        conn = Connection.objects.get(pk=google_id)
        story.check("agent-owned and unshared", conn.agent_id == world.eva.pk and conn.state == Connection.UNSHARED)
        state = browser.share_state(google_id)
        plain = browser.open_refresh(state)
        story.check("no host of its own", (plain.get("login_host"), plain.get("instance_url")) == ("", ""), plain)
        run = box1.stage(world.turn(box1), provider="google")
        story.check("unshared: --runner exits 3", run.rc == 3, run)
        r = browser.share(google_id, agents=[world.eva])
        story.check("share answers 200", r.status_code == 200, f"{r.status_code} {r.text[:800]}")
        digest = Connection.objects.get(pk=google_id).sealed_digest
        before = len(lab.calls())
        run = box1.stage(world.turn(box1), provider="google")
        calls = lab.calls()[before:]
        story.check("one refresh at Google's endpoint", len(calls) == 1
                    and calls[0]["url"] == web_google.TOKEN_URL, calls)
        _staged_ok(story, box1, run, "ya29.access_" + str(lab.provider()["n"]), provider="google", instance="")
        conn = Connection.objects.get(pk=google_id)
        story.check("nothing rotated: the refresh envelope is as shared", conn.sealed_digest == digest)
        story.check("access written", conn.access_sealed.startswith("zks1:"))
        _no_secret_at_rest(story, google_id, [GOOGLE_REFRESH])
        before = len(lab.calls())
        run = box2.stage(world.turn(box2), provider="google")
        story.check("box 2 reuses it", run.rc == 0 and len(lab.calls()) == before, run)
        r = browser.req("DELETE", f"/api/agents/eva/connections/{google_id}")
        story.check("disconnect answers 204", r.status_code == 204, f"{r.status_code} {r.text[:300]}")
        keep = box1.keep()
        story.check("the keeper revokes at Google", GOOGLE_REFRESH in lab.provider()["revoked"], keep)
        story.check("the row is gone", not Connection.objects.filter(pk=google_id).exists(), keep)
        kinds = set(Event.objects.values_list("kind", flat=True))
        for verb in ("connected", "shared", "token_issued", "refreshed", "disconnected", "revoked"):
            story.check(f"connections.{verb} is audited", f"connections.{verb}" in kinds, sorted(kinds))
        issued = Event.objects.filter(kind="connections.token_issued").first()
        story.check("token_issued names the machine", issued is not None and issued.payload.get("machine")
                    in ("eva-cloud-1", "eva-cloud-2"), issued.payload if issued else None)

    with story.step("N nothing reached the outside; no token reached a log"):
        story.check("no host but this one was tried after scenario B", not lab.blocked_hosts()[after_base_url_check:],
                    lab.blocked_hosts())
        minted = {c["form"].get("refresh_token") or c["form"].get("token") for c in lab.calls()} - {None}
        minted |= {f"00D!access_{i}" for i in range(1, lab.provider()["n"] + 1)}
        minted |= {f"5Aep_rotated_{i}" for i in range(1, lab.provider()["n"] + 1)}
        minted |= {f"ya29.access_{i}" for i in range(1, lab.provider()["n"] + 1)}
        logged = caplog.text
        leaked = sorted(t for t in minted if t in logged)
        story.check("no token in canopy-web's log", not leaked, leaked)

    report = story.render()
    print("\n" + report)
    results = os.environ.get("E2E_RESULTS")
    if results:
        Path(results).write_text(json.dumps(story.rows, indent=1))
    assert not story.failed(), f"{len(story.failed())} of {len(story.rows)} scenarios failed:\n{report}"
