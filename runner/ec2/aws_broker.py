#!/usr/bin/env python3
"""canopy-aws-broker: one hour of a NAMED read-only AWS role, for a runner on this machine.

docs/architecture/runners.md § Several runners on one machine. The instance role is
root's (an iptables rule closes IMDS to every runner uid), so a runner's turn cannot
call AWS as the machine — and must not: the instance role reads the runner seed
secrets. This root-owned daemon assumes, on a runner's behalf, ONLY the roles named in
`ROLES` (by default `labs-observe` -> `labs-jj-observe-readonly`, connect-labs
`infra/labs-observe-readonly.yml`: CloudWatch metrics, alarms and resource metadata,
with explicit denies on log events, object bodies, secrets, task commands/env and DB
content), and hands back one hour of that role's keys:

* a Unix socket at `/run/canopy-aws/sock` (mode 0666: any local process may CONNECT);
* the caller's uid from the kernel (`SO_PEERCRED`), never from the request, must be a
  runner in `/etc/canopy-runner/runners.json` — any other uid gets an error and no STS
  call;
* one JSON line per connection, one JSON line back:

      {"role": "labs-observe", "agent": "hal"}
          -> {"Version": 1, "AccessKeyId", "SecretAccessKey", "SessionToken",
              "Expiration", "RoleArn"}        (the AWS CLI's credential_process shape)
      anything else, or any failure -> {"error": "<reason>"}

Every session is stamped: `SourceIdentity` = the RUNNER (`canopy-runner-<name>`, which
the broker knows from the uid, so it cannot be forged), session tags `runner` and
`agent` (the agent the request names — a runner serves several agents and only the
caller knows which turn it is in), and `RoleSessionName` `<runner>.<agent>`. CloudTrail
therefore says which runner, and which agent it said it was, made every call.

The role's own trust policy must name this machine's instance role, and the instance
role must be allowed `sts:AssumeRole` on it (runner.cfn.yaml / fleet.cfn.yaml): neither
side alone lets anything in. Keys are cached per (uid, role, agent) until 5 minutes
before they expire, held in memory only, and never logged.

`aws_broker.py --install` (root): write and start `canopy-aws-broker.service` from this
file's own location. `cloud_runner.py --setup-runners` copies this file and the
kms_broker.py it imports (same sha) into /opt/canopy-runner/aws-broker/ and runs it on
every start of canopy-runners.service, so a box picks the broker up the next time the
updater restarts root's setup; an operator can run it on a live box without touching
any runner.
"""
from __future__ import annotations

import calendar
import json
import os
import pathlib
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kms_broker import (  # noqa: E402  (a sibling file in /opt/canopy-runner)
    ConnectionDeadline,
    Credentials,
    _imds,
    peer_uid,
    read_request,
    sigv4_headers,
)

SOCKET_PATH = os.environ.get("CANOPY_AWS_BROKER_SOCKET", "/run/canopy-aws/sock")
RUNNERS_FILE = os.environ.get("CANOPY_RUNNERS_FILE", "/etc/canopy-runner/runners.json")
UNIT_PATH = pathlib.Path(os.environ.get("CANOPY_SYSTEMD_DIR", "/etc/systemd/system")) / "canopy-aws-broker.service"
#: name -> role NAME in this machine's account (or a full role ARN). The only roles
#: this broker will ever assume. Override (JSON) with CANOPY_AWS_BROKER_ROLES.
DEFAULT_ROLES = {"labs-observe": "labs-jj-observe-readonly"}
SESSION_SECONDS = 3600
REFRESH_BEFORE = 300
MAX_CONNECTIONS = 16
CONNECTION_DEADLINE_SECONDS = 5.0
TIMEOUT = 15
_AGENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_STS_BAD = re.compile(r"[^\w+=,.@-]")
_NS = {"sts": "https://sts.amazonaws.com/doc/2011-06-15/"}


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} canopy-aws-broker: {msg}", flush=True)


def roles() -> dict[str, str]:
    raw = os.environ.get("CANOPY_AWS_BROKER_ROLES", "").strip()
    if not raw:
        return dict(DEFAULT_ROLES)
    try:
        out = json.loads(raw)
    except ValueError:
        return dict(DEFAULT_ROLES)
    return {str(k): str(v) for k, v in out.items()} if isinstance(out, dict) else dict(DEFAULT_ROLES)


def sts_name(text: str) -> str:
    out = _STS_BAD.sub("-", text)[:64]
    return out if len(out) >= 2 else (out + "--")[:2]


def load_runners(path: str | None = None) -> dict[int, dict]:
    """{uid: runner} from runners.json — only real runner uids."""
    try:
        doc = json.loads(open(path or RUNNERS_FILE).read())
    except (OSError, ValueError):
        return {}
    out = {}
    for runner in doc.get("runners") or []:
        uid = runner.get("uid")
        if isinstance(uid, int) and uid > 0:
            out[uid] = runner
    return out


class Sts:
    """AssumeRole with the instance role, SigV4-signed by hand (stdlib only)."""

    def __init__(self, creds: Credentials | None = None, opener=urllib.request.urlopen):
        self.creds = creds or Credentials(opener=opener)
        self._opener = opener
        self._account = ""

    def account(self) -> str:
        if not self._account:
            token = _imds("/latest/api/token", put=True, opener=self._opener,
                          headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
            info = json.loads(_imds("/latest/meta-data/iam/info", token=token, opener=self._opener))
            # arn:aws:iam::<account>:instance-profile/<name>
            self._account = str(info["InstanceProfileArn"]).split(":")[4]
        return self._account

    def role_arn(self, role: str) -> str:
        return role if role.startswith("arn:") else f"arn:aws:iam::{self.account()}:role/{role}"

    def assume(self, role_arn: str, *, session_name: str, source_identity: str,
               tags: dict[str, str]) -> dict:
        creds = self.creds.get()
        region = os.environ.get("CANOPY_AWS_STS_REGION", "us-east-1")
        host = os.environ.get("CANOPY_AWS_STS_HOST", "") or f"sts.{region}.amazonaws.com"
        params = {"Action": "AssumeRole", "Version": "2011-06-15", "RoleArn": role_arn,
                  "RoleSessionName": session_name, "DurationSeconds": str(SESSION_SECONDS),
                  "SourceIdentity": source_identity}
        for i, (k, v) in enumerate(sorted(tags.items()), start=1):
            params[f"Tags.member.{i}.Key"] = k
            params[f"Tags.member.{i}.Value"] = v
        body = urllib.parse.urlencode(params)
        headers = sigv4_headers(
            creds, region=region, service="sts", host=host, body=body,
            amz_date=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8", "Host": host})
        req = urllib.request.Request(f"https://{host}/", data=body.encode(), method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with self._opener(req, timeout=TIMEOUT) as resp:
                doc = ET.fromstring(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                code = ET.fromstring(exc.read()).findtext(".//sts:Code", default="", namespaces=_NS)
            except ET.ParseError:
                code = ""
            raise RuntimeError(f"STS AssumeRole answered {exc.code} {code}".strip()) from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"STS unreachable ({exc.reason})") from None
        c = doc.find(".//sts:Credentials", _NS)
        if c is None:
            raise RuntimeError("STS returned no credentials")
        return {"Version": 1,
                "AccessKeyId": c.findtext("sts:AccessKeyId", namespaces=_NS),
                "SecretAccessKey": c.findtext("sts:SecretAccessKey", namespaces=_NS),
                "SessionToken": c.findtext("sts:SessionToken", namespaces=_NS),
                "Expiration": c.findtext("sts:Expiration", namespaces=_NS),
                "RoleArn": role_arn}


def _expires_at(expiration: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(expiration[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return 0.0


class Cache:
    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[tuple, dict] = {}

    def get(self, key: tuple, now: float) -> dict | None:
        with self._lock:
            hit = self._items.get(key)
            if hit and _expires_at(hit["Expiration"]) - REFRESH_BEFORE > now:
                return hit
            return None

    def put(self, key: tuple, value: dict) -> None:
        with self._lock:
            self._items[key] = value


def answer(line: bytes, uid: int, runners: dict[int, dict], sts, cache: Cache,
           role_map: dict[str, str] | None = None, now: float | None = None) -> dict:
    """The reply to ONE request line from `uid`. Never raises."""
    runner = runners.get(uid)
    if runner is None:
        return {"error": f"uid {uid} is not a runner on this machine"}
    try:
        req = json.loads(line.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {"error": "the request is not one JSON line"}
    if not isinstance(req, dict):
        return {"error": "the request is not a JSON object"}
    role_map = roles() if role_map is None else role_map
    role = str(req.get("role") or "")
    if role not in role_map:
        return {"error": f"this broker does not vend a role named {role!r} "
                         f"(it vends: {', '.join(sorted(role_map)) or 'nothing'})"}
    agent = str(req.get("agent") or "unknown").lower()
    if not _AGENT.match(agent):
        return {"error": "agent must be an agent slug"}
    name = str(runner.get("name") or runner.get("id") or uid)
    key = (uid, role, agent)
    now = time.time() if now is None else now
    hit = cache.get(key, now)
    if hit:
        return hit
    try:
        out = sts.assume(sts.role_arn(role_map[role]), session_name=sts_name(f"{name}.{agent}"),
                         source_identity=sts_name(f"canopy-runner-{name}"),
                         tags={"runner": name, "agent": agent})
    except Exception as exc:  # noqa: BLE001 — every failure is an answer, never a crash
        return {"error": str(exc)[:300]}
    cache.put(key, out)
    _log(f"uid {uid} ({name}): {role} for agent {agent}, until {out.get('Expiration')}")
    return out


class Broker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, sts, runners_loader=load_runners, uid_of=peer_uid):
        self.sts, self.runners_loader, self.uid_of = sts, runners_loader, uid_of
        self.cache = Cache()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(path, _Handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b'{"error": "busy: too many connections"}\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: Broker = self.server  # type: ignore[assignment]
        deadline = time.monotonic() + CONNECTION_DEADLINE_SECONDS
        try:
            uid = server.uid_of(self.request)
        except OSError:
            uid = -1
        try:
            line = read_request(self.request, deadline)
        except ConnectionDeadline:
            return
        reply = ({"error": "request too long"} if line is None
                 else answer(line.strip(), uid, server.runners_loader(), server.sts, server.cache))
        if "error" in reply:
            _log(f"uid {uid}: {reply['error']}")
        try:
            self.request.settimeout(max(0.5, deadline - time.monotonic()))
            self.request.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass


def serve(path: str = SOCKET_PATH) -> None:
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = Broker(path, Sts())
    os.chmod(path, 0o666)   # any local process may CONNECT; only a runner uid is answered
    _log(f"listening on {path}; vends {', '.join(sorted(roles()))}")
    server.serve_forever()


UNIT = """[Unit]
Description=Canopy AWS broker: one hour of a named read-only AWS role, for this machine's runners
After=canopy-runners.service network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-/etc/canopy-runner/box.env
ExecStart=/usr/bin/python3 {path}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def install(*, run=subprocess.run, unit_path: pathlib.Path | None = None) -> bool:
    """ROOT: write canopy-aws-broker.service for this file and start it. Idempotent;
    restarts the broker only when its unit changed. Returns whether it changed."""
    unit_path = unit_path or UNIT_PATH
    text = UNIT.format(path=os.path.abspath(__file__))
    try:
        changed = unit_path.read_text() != text
    except OSError:
        changed = True
    if changed:
        unit_path.write_text(text)
        run(["systemctl", "daemon-reload"], capture_output=True)
    run(["systemctl", "enable", "--now", "canopy-aws-broker.service"], capture_output=True)
    if changed:
        run(["systemctl", "restart", "canopy-aws-broker.service"], capture_output=True)
    return changed


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("canopy-aws-broker runs as root (it holds the instance role)")
    if sys.argv[1:2] == ["--install"]:
        install()
        sys.exit(0)
    serve()
