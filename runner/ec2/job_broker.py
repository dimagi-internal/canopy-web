#!/usr/bin/env python3
"""canopy-job-broker: start a plain job as `canopy-job`, for a runner on this machine.

A runner runs under NoNewPrivileges, so it can't become another user itself, and that
protects every model turn it runs. This root daemon does that one thing for it, as
kms_broker.py and aws_broker.py do theirs:

* a Unix socket at `/run/canopy-jobs/sock` (mode 0666: any local process may CONNECT);
* the caller's uid from the kernel (`SO_PEERCRED`), never from the request, must be a
  runner in `/etc/canopy-runner/runners.json`. Any other uid gets an error and nothing else;
* one JSON line per connection, its body (if any) straight after it, and only these:

      status                                      -> {ok, systemd, supported, job_user, locked, running}
      export       {turn, size} + a tar            -> {ok}   unpack into a new job folder
      write_token  {turn, provider, size} + bytes  -> {ok}   size 0 removes that copy
      start        {turn, slug, run, timeout_seconds} -> frames: output, then the exit
      stop         {turn}                          -> {ok}   the unit stops, TERM then KILL
      cleanup                                      -> {ok}   stop this caller's job, remove all
      anything else, or any failure                -> {"error": "<a plain sentence>"}

No request names a path, a user, a property or an environment: unknown keys are refused, and
each folder, the unit's sandbox and the environment are built here from a validated turn id.
So a caller with a runner's uid gains nothing beyond running code as `canopy-job`, which has
less than that runner has. Everything run here runs as that user; the only commands root
itself runs are `systemd-run` and `systemctl` on a unit named from that turn id.

`start` answers in frames, a type byte and a 4-byte length: `O` the job's output, `X` a JSON
exit, always last. Framing is what keeps a job from printing its own exit. If the caller goes
away, its job is stopped.

Only one job runs at a time here, and only the runner that started it can stop it. (The
runners' own box-wide flock, `cloud_runner.JOB_LOCK_FILE`, is what orders them politely.)

`job_broker.py --install` (root): write and start `canopy-job-broker.service` from this file's
own location. `cloud_runner.py --setup-runners` copies this file to
/opt/canopy-runner/job-broker/ and runs that on every start of canopy-runners.service.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import pwd
import re
import select
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

SOCKET_PATH = os.environ.get("CANOPY_JOB_BROKER_SOCKET", "/run/canopy-jobs/sock")
RUNNERS_FILE = os.environ.get("CANOPY_RUNNERS_FILE", "/etc/canopy-runner/runners.json")
EGRESS_STATE_FILE = "/etc/canopy-runner/egress.state.json"
BOX_ENV_FILE = "/etc/canopy-runner/box.env"
UNIT_PATH = pathlib.Path("/etc/systemd/system/canopy-job-broker.service")
JOBS_ROOT = "/var/lib/canopy-jobs"
JOB_USER = "canopy-job"
JOB_PATH = "/usr/local/bin:/usr/bin:/bin"
#: What the job's environment holds, in this order, and nothing else.
SIX = ("PATH", "HOME", "LANG", "CANOPY_AGENT_SLUG", "CANOPY_TURN_ID", "CANOPY_JOB_TOKENS")
#: `--expand-environment` arrived in 254; the box's Ubuntu 24.04 has 255.
MIN_SYSTEMD = 254
MAX_HEAD = 256 * 1024
MAX_EXPORT = 256 * 1024 * 1024
MAX_TOKEN = 64 * 1024
MAX_ARGV_WORDS = 256
MAX_ARGV_BYTES = 128 * 1024
MAX_TIMEOUT_SECONDS = 120 * 60
MAX_CONNECTIONS = 16
HEAD_DEADLINE_SECONDS = 5.0
BODY_DEADLINE_SECONDS = 120.0

_TURN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_SLUG = re.compile(r"[a-z0-9-]{0,64}")


def _log(msg: str) -> None:
    print(f"[job-broker] {msg}", flush=True)


class Refused(Exception):
    """A request this broker won't do, with the sentence the caller reads."""


# ── what may be asked: pure ─────────────────────────────────────────────────

def _turn(value) -> None:
    if not isinstance(value, str) or not _TURN.fullmatch(value):
        raise Refused("That isn't a turn id.")


def _size(value, *, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise Refused("That size isn't allowed.")


def _run(value) -> None:
    if (not isinstance(value, list) or not value or len(value) > MAX_ARGV_WORDS
            or not all(isinstance(w, str) and w and "\0" not in w for w in value)
            or sum(len(w.encode("utf-8", "replace")) for w in value) > MAX_ARGV_BYTES):
        raise Refused("`run` must be a list of words.")
    first = value[0]
    if first.startswith(("/", "-")) or ".." in first.split("/") or "=" in first:
        raise Refused("The program to run must be a bare name or a path inside the repo.")


def validate_request(req) -> dict:
    """`req` if it is exactly one of the operations, or `Refused`. Exact keys: no request
    can carry a path, a user, a property or an environment past this."""
    if not isinstance(req, dict) or not isinstance(req.get("op"), str) or req["op"] not in _OPS:
        raise Refused("That isn't an operation of this broker.")
    if set(req) != {"op", *_OPS[req["op"]]}:
        raise Refused(f"That isn't what {req['op']} takes.")
    op = req["op"]
    if "turn" in req:
        _turn(req["turn"])
    if op == "export":
        _size(req["size"], low=1, high=MAX_EXPORT)
    elif op == "write_token":
        if not isinstance(req["provider"], str) or not _PROVIDER.fullmatch(req["provider"]):
            raise Refused("That isn't a provider name.")
        _size(req["size"], low=0, high=MAX_TOKEN)
    elif op == "start":
        if not isinstance(req["slug"], str) or not _SLUG.fullmatch(req["slug"]):
            raise Refused("That isn't an agent name.")
        _run(req["run"])
        _size(req["timeout_seconds"], low=1, high=MAX_TIMEOUT_SECONDS)
    return req


_OPS = {"status": (), "cleanup": (), "stop": ("turn",), "export": ("turn", "size"),
        "write_token": ("turn", "provider", "size"), "start": ("turn", "slug", "run", "timeout_seconds")}


# ── what gets run: pure ─────────────────────────────────────────────────────

def job_env(turn: str, slug: str, root: str) -> dict:
    job = f"{root}/{turn}"
    return dict(zip(SIX, (JOB_PATH, f"{job}/home", "C.UTF-8", slug, turn, f"{job}/tokens"), strict=True))


#: Not under /home, so ProtectHome doesn't reach them; a `-` so a box without one still starts.
#: A runner's WORK_DIR, AGENT_ROOT and ~/.canopy are all under /home (`runner_env`), and /opt/agents
#: is the default AGENT_ROOT of a box that isn't. `/dev/shm` and `/dev/mqueue`: PrivateDevices
#: binds the host's in, and they would be the one writable place shared between jobs.
INACCESSIBLE = ("/dev/shm", "/dev/mqueue", "/opt/agents", "/opt/canopy-runner", "/opt/canopy-web",
                "/etc/canopy-runner", "/run/canopy", "/run/canopy-kms", "/run/canopy-aws", "/run/canopy-jobs")


def job_properties(turn: str, slug: str, timeout_seconds: int, root: str) -> list:
    job = f"{root}/{turn}"
    env = job_env(turn, slug, root)
    return [
        "NoNewPrivileges=yes", "ProtectHome=yes", "ProtectSystem=strict", f"ReadWritePaths={job}",
        "PrivateTmp=yes", "PrivateDevices=yes", "ProtectProc=invisible", "ProcSubset=pid",
        "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_INET AF_INET6",
        # `localhost` covers the DNS stub too, so that one address is allowed back: an allow
        # wins. The stub's other ports, and the box's own addresses, which this list doesn't
        # name, are closed by the owner rule in `cloud_runner.job_rules`.
        "IPAddressDeny=169.254.169.254 fd00:ec2::254 localhost", "IPAddressAllow=127.0.0.53",
        "KillMode=control-group", f"RuntimeMaxSec={timeout_seconds}", "TimeoutStopSec=10s",
        # MemorySwapMax: the box has swap, which would otherwise take a job past MemoryMax.
        "MemoryMax=2G", "MemorySwapMax=0", "CPUQuota=100%", "TasksMax=256", "LimitFSIZE=5G",
        f"WorkingDirectory={job}/repo",
        *[f"InaccessiblePaths=-{path}" for path in INACCESSIBLE],
        *[f"Environment={k}={v}" for k, v in env.items()],
    ]


def systemd_run_argv(turn: str, slug: str, run: list, timeout_seconds: int, *, root: str = JOBS_ROOT) -> list:
    """The one command that runs a job. `run`'s words stay separate arguments all the way:
    no shell, and `--expand-environment=no` keeps systemd from reading `$X` in them. `env -i`
    because systemd adds USER, SHELL, INVOCATION_ID and more of its own, and a job's
    environment is the six."""
    env = job_env(turn, slug, root)
    return ["systemd-run", "--wait", "--pipe", "--collect", "--quiet", "--expand-environment=no",
            "--service-type=exec", f"--unit=canopy-job-{turn}", f"--uid={JOB_USER}", f"--gid={JOB_USER}",
            *[f"--property={p}" for p in job_properties(turn, slug, timeout_seconds, root)],
            "--", "/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()], *run]


def unit_name(turn: str) -> str:
    return f"canopy-job-{turn}.service"


def unpack_argv(dest: str) -> list:
    return ["tar", "-x", "--no-same-owner", "--no-same-permissions", "-C", dest]


def cleanup_argvs(root: str) -> list:
    """`chmod` first: a folder a job made read-only would stay otherwise."""
    return [["chmod", "-R", "u+rwx", root], ["find", root, "-mindepth", "1", "-delete"]]


TOKEN_SCRIPT = 'umask 077; cat > "$1.tmp" && mv -f "$1.tmp" "$1"'


def exit_info(rc: int, elapsed: float, timeout_seconds: int) -> dict:
    """systemd-run says only "1" for a unit that RuntimeMaxSec ended (and printing its result
    would mix with the job's own output), so a failure at the limit reads as the limit."""
    return {"rc": rc, "timed_out": rc != 0 and elapsed >= timeout_seconds}


def box_locked(state: dict, box: dict) -> bool:
    """Whether any runner on this box is egress-locked: then no job runs here at all, since a
    job can't use a runner's proxy and must not walk past its lock."""
    return (any(bool(v) for v in (state.get("applied") or {}).values())
            or bool(str(box.get("RUNNER_EGRESS", "")).strip())
            or bool(str(box.get("RUNNER_DATA_MODE", "")).strip()))


def systemd_version(text: str) -> int:
    found = re.match(r"systemd (\d+)", text or "")
    return int(found.group(1)) if found else 0


def frame(kind: bytes, data: bytes) -> bytes:
    return kind + struct.pack(">I", len(data)) + data


# ── the box's doors: each stubbed in tests ──────────────────────────────────

def as_job_user(args, input=None):
    """`args` as the job user, with nothing of root's: no groups, no environment."""
    return subprocess.run(args, input=input, capture_output=True, timeout=300, cwd="/", user=JOB_USER,
                          group=JOB_USER, extra_groups=[], env={"PATH": JOB_PATH})


def spawn_job(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd="/", start_new_session=True)


def stop_unit(turn: str) -> None:
    subprocess.run(["systemctl", "stop", "--no-block", unit_name(turn)], capture_output=True, timeout=30)


def load_runner_uids(path: str | None = None) -> set:
    try:
        doc = json.loads(open(path or RUNNERS_FILE).read())
    except (OSError, ValueError):
        return set()
    return {r["uid"] for r in doc.get("runners") or [] if type(r.get("uid")) is int and r["uid"] > 0}


def read_box_state() -> dict:
    try:
        state = json.loads(open(EGRESS_STATE_FILE).read())
    except (OSError, ValueError):
        state = {}
    box = {}
    try:
        for line in open(BOX_ENV_FILE):
            key, sep, value = line.strip().partition("=")
            if sep:
                box[key] = value
    except OSError:
        pass
    try:
        version = subprocess.run(["systemctl", "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        version = ""
    try:
        pwd.getpwnam(JOB_USER)
        user = True
    except KeyError:
        user = False
    return {"locked": box_locked(state, box), "systemd": systemd_version(version), "job_user": user}


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


# ── the daemon ──────────────────────────────────────────────────────────────

class JobBroker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, *, root: pathlib.Path = pathlib.Path(JOBS_ROOT), as_job=as_job_user, spawn=spawn_job,
                 stop=stop_unit, uid_of=peer_uid, runner_uids=load_runner_uids, box_state=read_box_state):
        self.root = pathlib.Path(root)
        self.as_job, self.spawn, self.stop, self.uid_of = as_job, spawn, stop, uid_of
        self.runner_uids, self.box_state = runner_uids, box_state
        #: The job this box holds: {turn, uid, proc}. One at a time, whoever asks.
        self._job: dict | None = None
        self._lock = threading.Lock()
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

    def _running(self) -> dict | None:
        job = self._job
        return job if job and job.get("proc") is not None and job["proc"].poll() is None else None

    def _held_by(self, req: dict, uid: int) -> dict:
        job = self._job
        if not job or job["turn"] != req["turn"] or job["uid"] != uid:
            raise Refused("That job isn't this runner's on this machine.")
        return job

    def dispatch(self, req: dict, body: bytes, conn, uid: int):
        """One validated request. A dict is the reply; `start` answers on `conn` and returns None."""
        return getattr(self, f"_op_{req['op']}")(req, body, conn, uid)

    def _op_status(self, req, body, conn, uid) -> dict:
        state = self.box_state()
        running = self._running()
        return {"ok": True, "systemd": state["systemd"], "supported": state["systemd"] >= MIN_SYSTEMD,
                "job_user": state["job_user"], "locked": state["locked"],
                "running": running["turn"] if running else ""}

    def _make(self, args, input=None, sentence="Couldn't set up the job's folder.") -> None:
        if self.as_job(args, input=input).returncode != 0:
            raise Refused(sentence)

    def _op_export(self, req, body, conn, uid) -> dict:
        turn = req["turn"]
        with self._lock:
            if self._running():
                raise Refused("Another job is running on this machine.")
            self._job = {"turn": turn, "uid": uid, "proc": None}
        job = self.root / turn
        for path in (job, job / "repo", job / "home", job / "tokens"):
            self._make(["mkdir", "-m", "700", str(path)])
        self._make(unpack_argv(str(job / "repo")), input=body, sentence="Couldn't unpack the repo's files.")
        return {"ok": True}

    def _op_write_token(self, req, body, conn, uid) -> dict:
        self._held_by(req, uid)
        dest = str(self.root / req["turn"] / "tokens" / f"{req['provider']}.json")
        if req["size"] == 0:
            self._make(["rm", "-f", dest], sentence="Couldn't remove the job's copy of that token.")
        else:
            self._make(["sh", "-c", TOKEN_SCRIPT, "sh", dest], input=body,
                       sentence="Couldn't write the job's copy of that token.")
        return {"ok": True}

    def _op_stop(self, req, body, conn, uid) -> dict:
        self._held_by(req, uid)
        self.stop(req["turn"])
        return {"ok": True}

    def _op_cleanup(self, req, body, conn, uid) -> dict:
        with self._lock:
            running = self._running()
            if running and running["uid"] != uid:
                raise Refused("Another runner's job is running on this machine.")
        if running:
            self.stop(running["turn"])
            deadline = time.monotonic() + 30
            while running["proc"].poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if running["proc"].poll() is None:
                raise Refused("The job didn't stop.")
        self.clear_files()
        self._job = None
        return {"ok": True}

    def clear_files(self) -> None:
        for args in cleanup_argvs(str(self.root)):
            self.as_job(args)

    def _op_start(self, req, body, conn, uid) -> None:
        send_lock = threading.Lock()

        def send(kind, data) -> bool:
            with send_lock:
                try:
                    conn.sendall(frame(kind, data))
                    return True
                except OSError:
                    return False

        def refuse(sentence):
            send(b"X", json.dumps({"error": sentence}).encode())

        turn, timeout = req["turn"], req["timeout_seconds"]
        state = self.box_state()
        if state["locked"]:
            return refuse("A runner on this machine has its network locked, so a job can't run here.")
        with self._lock:
            try:
                job = self._held_by(req, uid)
            except Refused as exc:
                return refuse(str(exc))
            if self._running():
                return refuse("Another job is running on this machine.")
            try:
                job["proc"] = proc = self.spawn(systemd_run_argv(turn, req["slug"], req["run"], timeout,
                                                                 root=str(self.root)))
            except OSError as exc:
                return refuse(f"The job couldn't start ({type(exc).__name__}).")
        started = time.monotonic()
        _log(f"uid {uid}: started {unit_name(turn)}")

        def pump():
            fd = proc.stdout.fileno()
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                send(b"O", chunk)

        pumping = threading.Thread(target=pump, daemon=True)
        pumping.start()
        conn.setblocking(True)
        conn.settimeout(None)
        gone = False
        while proc.poll() is None:
            if not gone and select.select([conn], [], [], 0.2)[0]:
                try:
                    gone = conn.recv(1, socket.MSG_DONTWAIT) == b""
                except (BlockingIOError, InterruptedError):
                    pass
                except OSError:
                    gone = True
                if gone:
                    _log(f"uid {uid}: its runner went away; stopping {unit_name(turn)}")
                    self.stop(turn)
            elif gone:
                time.sleep(0.2)
        # The unit has ended (`--wait`), and control-group kill took what it left, which is what
        # closes the pipe. The stop is for anything else: nothing here should keep the pipe open.
        self.stop(turn)
        pumping.join(timeout=10)
        send(b"X", json.dumps(exit_info(proc.returncode, time.monotonic() - started, timeout)).encode())
        return None


def _read_head(sock, deadline: float) -> tuple:
    """The request line and whatever of the body came with it. A deadline for the whole line:
    a per-read timeout would let a client drip a byte at a time and hold a slot."""
    buf = b""
    while b"\n" not in buf:
        if len(buf) > MAX_HEAD:
            return None, b""
        left = deadline - time.monotonic()
        if left <= 0:
            return None, b""
        sock.settimeout(left)
        try:
            chunk = sock.recv(65536)
        except (TimeoutError, OSError):
            return None, b""
        if not chunk:
            return None, b""
        buf += chunk
    line, _, rest = buf.partition(b"\n")
    return line, rest


def _read_body(sock, rest: bytes, size: int, deadline: float) -> bytes:
    parts, have = [rest], len(rest)
    while have < size:
        left = deadline - time.monotonic()
        if left <= 0:
            raise Refused("The request's body didn't arrive in time.")
        sock.settimeout(left)
        try:
            chunk = sock.recv(min(1 << 20, size - have))
        except (TimeoutError, OSError):
            raise Refused("The request's body didn't arrive.") from None
        if not chunk:
            raise Refused("The request's body was cut short.")
        parts.append(chunk)
        have += len(chunk)
    return b"".join(parts)[:size]


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: JobBroker = self.server  # type: ignore[assignment]
        conn = self.request
        try:
            uid = server.uid_of(conn)
        except OSError:
            uid = -1
        line, rest = _read_head(conn, time.monotonic() + HEAD_DEADLINE_SECONDS)
        if uid not in server.runner_uids():
            reply = {"error": f"uid {uid} is not a runner on this machine"}
        elif line is None:
            reply = {"error": "No whole request arrived."}
        else:
            try:
                req = validate_request(json.loads(line.decode()))
                body = b""
                if req.get("size"):
                    body = _read_body(conn, rest, req["size"], time.monotonic() + BODY_DEADLINE_SECONDS)
                reply = server.dispatch(req, body, conn, uid)
            except (ValueError, UnicodeDecodeError):
                reply = {"error": "That isn't a request."}
            except Refused as exc:
                reply = {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - one request never takes the broker down
                _log(f"uid {uid}: {type(exc).__name__}: {exc}")
                reply = {"error": "The job broker hit an error."}
        if reply is None:
            return
        if "error" in reply:
            _log(f"uid {uid}: {reply['error']}")
        try:
            conn.settimeout(5)
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass


def serve(path: str = SOCKET_PATH) -> None:
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    # This process holds every running job's stream, so a job from before it is nobody's.
    subprocess.run(["systemctl", "stop", "canopy-job-*.service"], capture_output=True, timeout=60)
    server = JobBroker(path)
    try:
        server.clear_files()
    except (OSError, KeyError, subprocess.SubprocessError) as exc:
        _log(f"could not clear {JOBS_ROOT} at start: {exc}")
    os.chmod(path, 0o666)   # any local process may CONNECT; only a runner uid is answered
    _log(f"listening on {path}")
    server.serve_forever()


UNIT = """[Unit]
Description=Canopy job broker: start a plain job as canopy-job, for this machine's runners
After=canopy-runners.service

[Service]
Type=simple
# Its code's hash, so that new code is a changed unit, and `install` restarts it.
Environment=CANOPY_JOB_BROKER_SHA={sha}
ExecStart=/usr/bin/python3 {path}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def install(*, run=subprocess.run, unit_path: pathlib.Path | None = None) -> bool:
    """ROOT: write canopy-job-broker.service for this file and start it. Idempotent; restarts
    only when its code or unit changed. `--setup-runners` runs on a box with no turn running.
    Returns whether it changed."""
    unit_path = unit_path or UNIT_PATH
    here = os.path.abspath(__file__)
    text = UNIT.format(path=here, sha=hashlib.sha256(pathlib.Path(here).read_bytes()).hexdigest())
    try:
        changed = unit_path.read_text() != text
    except OSError:
        changed = True
    if changed:
        unit_path.write_text(text)
        run(["systemctl", "daemon-reload"], capture_output=True)
    run(["systemctl", "enable", "--now", "canopy-job-broker.service"], capture_output=True)
    if changed:
        run(["systemctl", "restart", "canopy-job-broker.service"], capture_output=True)
    return changed


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("canopy-job-broker runs as root (it starts units)")
    if sys.argv[1:2] == ["--install"]:
        install()
        sys.exit(0)
    serve()
