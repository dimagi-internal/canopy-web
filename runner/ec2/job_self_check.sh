#!/usr/bin/env bash
# Prove, on a real box, what the tests can't: that a plain job's unit is refused everything
# it must not reach. Run once as root, after the box's setup has run:
#
#   sudo bash /opt/canopy-web/runner/ec2/job_self_check.sh
#
# It starts real units through the job broker, as a runner's uid, and what's inside them
# reports what it could reach. It reads no secret's content (a refused read is the pass) and
# leaves nothing behind: the broker's cleanup empties the jobs root. It stops at once if a job
# is running. One PASS or FAIL per line, SKIP where the box doesn't have the thing. Exit 0 only
# with no FAIL.
set -uo pipefail

JOB_USER=canopy-job
JOBS_ROOT=/var/lib/canopy-jobs
LOCK=/run/canopy/job.lock
BROKER=/run/canopy-jobs/sock
RUNNERS_FILE=/etc/canopy-runner/runners.json
ETC=/etc/canopy-runner
WORK=""
FAILS=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
skip() { printf 'SKIP  %s\n' "$1"; }

cleanup() {
  [ -n "$WORK" ] && rm -rf "$WORK"
}
trap cleanup EXIT

if [ "$(id -u)" != 0 ]; then
  echo "Run this as root." >&2
  exit 2
fi
if ! id -u "$JOB_USER" >/dev/null 2>&1; then
  fail "$JOB_USER exists (a sensitive-data box has none, and never runs a job)"
  exit 1
fi
if [ -e "$LOCK" ] && ! flock -n "$LOCK" true; then
  echo "A job is running on this machine: try again when it has ended." >&2
  exit 2
fi
rows="$(python3 - "$RUNNERS_FILE" <<'PY' 2>/dev/null
import json, sys
for r in json.load(open(sys.argv[1])).get("runners") or []:
    print(r["id"], r["user"], r["home"], r["uid"])
PY
)"
if [ -z "$rows" ]; then
  fail "no runners in $RUNNERS_FILE"
  exit 1
fi
first_user="$(awk 'NR==1 {print $2}' <<<"$rows")"
job_uid="$(id -u "$JOB_USER")"
WORK="$(mktemp -d)"
chmod 755 "$WORK"

echo "== the user, its folders and the lock"
shell="$(getent passwd "$JOB_USER" | cut -d: -f7)"
case "$shell" in
  */nologin|*/false) pass "$JOB_USER has no login shell ($shell)" ;;
  *) fail "$JOB_USER has a login shell: $shell" ;;
esac
if [ "$job_uid" -lt 1000 ] && [ "$job_uid" -gt 0 ]; then
  pass "$JOB_USER is a system user (uid $job_uid)"
else
  fail "$JOB_USER has uid $job_uid: expected a system user"
fi
if [ "$(id -Gn "$JOB_USER" | wc -w)" = 1 ]; then
  pass "$JOB_USER is in no other group"
else
  fail "$JOB_USER is in other groups: $(id -Gn "$JOB_USER")"
fi
if [ "$(stat -c '%U %a' "$JOBS_ROOT" 2>/dev/null)" = "$JOB_USER 700" ]; then
  pass "$JOBS_ROOT is $JOB_USER's, 0700"
else
  fail "$JOBS_ROOT should be $JOB_USER's and 0700: $(stat -c '%U %a' "$JOBS_ROOT" 2>&1)"
fi
if [ "$(stat -c '%U %a' "$LOCK" 2>/dev/null)" = "root 644" ]; then
  pass "$LOCK is root's, 0644"
else
  fail "$LOCK should be root's and 0644: $(stat -c '%U %a' "$LOCK" 2>&1)"
fi
if [ -e /etc/sudoers.d/canopy-jobs ]; then
  fail "/etc/sudoers.d/canopy-jobs exists: no runner should be able to sudo"
else
  pass "no sudo rule for $JOB_USER"
fi

echo "== the runners and the rules"
while read -r id user _home _uid; do
  [ -n "${id:-}" ] || continue
  nnp="$(systemctl show -p NoNewPrivileges --value "canopy-runner@$id.service" 2>/dev/null)"
  if [ "$nnp" = "yes" ]; then
    pass "canopy-runner@$id keeps NoNewPrivileges"
  else
    fail "canopy-runner@$id has NoNewPrivileges=$nnp"
  fi
  if sudo -n -u "$user" python3 -c 'import yaml' 2>/dev/null; then
    pass "$user has PyYAML"
  else
    fail "$user can't import yaml"
  fi
done <<<"$rows"
if iptables -S OUTPUT 2>/dev/null | grep -qF -- "-j CANOPY-JOB" \
    && iptables -S CANOPY-JOB 2>/dev/null | grep -qF -- "--uid-owner $job_uid"; then
  pass "CANOPY-JOB holds rules for uid $job_uid, and OUTPUT jumps to it"
else
  fail "CANOPY-JOB isn't in place for uid $job_uid"
fi
if [ "$(systemctl is-enabled canopy-runners.service 2>/dev/null)" = enabled ]; then
  pass "canopy-runners.service runs every boot, so the rules come back after a reboot"
else
  fail "canopy-runners.service isn't enabled, so the rules won't survive a reboot"
fi
if [ "$(systemctl is-active canopy-job-broker.service 2>/dev/null)" = active ] && [ -S "$BROKER" ]; then
  pass "canopy-job-broker is running, at $BROKER"
else
  fail "canopy-job-broker isn't running"
  echo; echo "$FAILS check(s) FAILED."; exit 1
fi

# The paths a job must be refused, one per line. Every runner's own.
{
  printf '%s\n' /home /root "$ETC" "$ETC/box.env" "$ETC/egress.state.json" /run/canopy "$LOCK" \
    /opt/canopy-runner /opt/canopy-runner/runner.env /opt/canopy-runner/runner-token.json /opt/agents \
    /opt/canopy-web /etc/shadow /etc/sudoers /dev/shm /dev/mqueue
  while read -r id _user home uid; do
    [ -n "${id:-}" ] || continue
    printf '%s\n' "$home" "$home/.canopy-runner/runner-token.json" "$home/.canopy-runner/in-flight" \
      "$home/.canopy" "$home/.canopy/connections" "$home/.canopy/device-key/sw.pem" \
      "$home/.canopy-cloud-runner.json" "$home/work" "$home/agents" "$home/.claude" \
      "$ETC/$id.env" "$ETC/$id.gog-secret" "/run/canopy/$uid"
    for envfile in "$home"/.*/.env; do
      [ -e "$envfile" ] && printf '%s\n' "$envfile"
    done
  done <<<"$rows"
} | sort -u > "$WORK/refused.txt"
printf '%s\n' /run/canopy-kms/sock /run/canopy-aws/sock "$BROKER" > "$WORK/sockets.txt"
while read -r id _user _home uid; do
  [ -n "${id:-}" ] || continue
  printf '%s %s\n' "$id" "$((20000 + uid % 10000))"
done <<<"$rows" > "$WORK/proxy-ports.txt"
hostname -I 2>/dev/null | awk '{print $1}' > "$WORK/own-ip.txt"

# What runs inside the unit, as the job would. It only reports; this script's client prints it.
cat > "$WORK/probe.py" <<'PY'
import errno, json, os, socket, sys

def pass_(what): print("PASS  " + what, flush=True)
def fail(what): print("FAIL  " + what, flush=True)
def lines(name): return [x.strip() for x in open(name) if x.strip()]

want = {"PATH", "HOME", "LANG", "CANOPY_AGENT_SLUG", "CANOPY_TURN_ID", "CANOPY_JOB_TOKENS"}
got = set(os.environ) - {"LC_CTYPE"}
(pass_ if got == want else fail)(f"a job's environment is exactly the six ({sorted(got)})")
(pass_ if os.getcwd().endswith("/repo") else fail)(f"a job runs in its export ({os.getcwd()})")
(pass_ if os.geteuid() != 0 and os.path.basename(os.environ["HOME"]) == "home" else fail)(f"a job runs as uid {os.geteuid()}")
(pass_ if os.environ["PATH"] == "/usr/local/bin:/usr/bin:/bin" else fail)("a job's PATH is the fixed one")

tokens = os.environ["CANOPY_JOB_TOKENS"] + "/self-check.json"
mode = os.stat(tokens).st_mode & 0o777
(pass_ if mode == 0o600 else fail)(f"its token copy is 0600 ({oct(mode)})")

for path in lines("refused.txt"):
    try:
        if os.path.isdir(path):
            names = os.listdir(path)
            if path == "/home" and not names:
                pass_("/home is empty to a job")
            else:
                fail(f"a job can list {path} ({len(names)} names)")
        else:
            with open(path, "rb"):
                pass
            fail(f"a job can read {path}")
    except OSError as exc:
        pass_(f"a job can't read {path} ({errno.errorcode.get(exc.errno, exc.errno)})")

for path in ("/dev/shm/x", "/var/lib/x", "/etc/x", "/usr/x"):
    try:
        with open(path, "w"):
            fail(f"a job can write {path}")
    except OSError:
        pass_(f"a job can't write {path}")
for name in ("/tmp/x", os.environ["HOME"] + "/x"):
    try:
        with open(name, "w") as f:
            f.write("x")
        pass_(f"a job can write {name}, its own")
    except OSError as exc:
        fail(f"a job can't write {name}, its own: {exc}")

for path in lines("sockets.txt"):
    try:
        s = socket.socket(socket.AF_UNIX)
    except OSError:
        pass_(f"a job can't open a Unix socket at all, so not {path}")
        continue
    try:
        s.settimeout(3)
        s.connect(path)
        s.sendall(b'{"op": "status"}\n')
        said = s.recv(4096)
        (pass_ if b"not a runner" in said else fail)(f"{path} answered a job: {said[:80]!r}")
    except OSError:
        pass_(f"a job can't reach {path}")

def closed(what, host, port):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(family)
    s.settimeout(4)
    try:
        s.connect((host, port))
        fail(f"{what} was reachable ({host}:{port})")
    except TimeoutError:
        fail(f"{what} gave no refusal, only silence ({host}:{port})")
    except OSError as exc:
        pass_(f"{what} is refused ({host}:{port}, {errno.errorcode.get(exc.errno, exc.errno)})")

closed("IMDSv4", "169.254.169.254", 80)
if os.path.exists("/proc/net/if_inet6") or os.path.exists("/sys/class/net/lo/address"):
    try:
        closed("IMDSv6", "fd00:ec2::254", 80)
        closed("IPv6 localhost", "::1", 48731)
    except OSError:
        pass_("no IPv6 for a job here")
closed("localhost", "127.0.0.1", 48731)
closed("the rest of loopback", "127.0.0.2", 48731)
closed("the DNS stub's other ports", "127.0.0.53", 48731)
for ip in lines("own-ip.txt"):
    closed("the box's own address", ip, 48731)
for row in lines("proxy-ports.txt"):
    who, port = row.split()
    closed(f"{who}'s egress proxy port", "127.0.0.1", int(port))

if json.load(open("expect.json"))["locked"]:
    print("SKIP  DNS: a runner on this box is locked", flush=True)
else:
    try:
        socket.getaddrinfo("canopy.dimagi.com", 443)
        pass_("DNS works for a job (getaddrinfo canopy.dimagi.com)")
    except OSError as exc:
        fail(f"DNS doesn't work for a job: {exc}")

# Other users' processes. ProtectProc=invisible hides them entirely.
others = []
for name in os.listdir("/proc"):
    if name.isdigit():
        try:
            if os.stat(f"/proc/{name}").st_uid != os.geteuid():
                others.append(name)
        except OSError:
            pass
(pass_ if not others else fail)(f"a job sees no other user's process in /proc ({len(others)})")

# The limits, as the cgroup itself states them.
path = open("/proc/self/cgroup").read().strip().split("::")[-1]
for name, value in (("memory.max", str(2 * 1024 ** 3)), ("memory.swap.max", "0"), ("pids.max", "256")):
    try:
        have = open(f"/sys/fs/cgroup{path}/{name}").read().strip()
        (pass_ if have == value else fail)(f"{name} is {value} ({have})")
    except OSError as exc:
        fail(f"could not read {name}: {exc}")

# And TasksMax actually refusing a fork.
kids, refused = [], False
try:
    for _ in range(300):
        pid = os.fork()
        if pid == 0:
            import time
            time.sleep(30)
            os._exit(0)
        kids.append(pid)
except OSError:
    refused = True
for pid in kids:
    os.kill(pid, 9)
    os.waitpid(pid, 0)
(pass_ if refused else fail)(f"TasksMax refused a fork (after {len(kids)})")

# Left running on purpose: the client checks that nothing survives the job's end.
if os.fork() == 0:
    os.setsid()
    import time
    time.sleep(120)
    os._exit(0)
print("PROBE-DONE", flush=True)
PY
cat > "$WORK/memory.py" <<'PY'
# Touch 3 GiB: MemoryMax=2G with no swap must end this.
chunks = []
for _ in range(3 * 1024):
    chunks.append(bytearray(b"x") * (1024 * 1024))
print("ALLOCATED", flush=True)
PY
cat > "$WORK/client.py" <<'PY'
import io, json, os, socket, struct, subprocess, sys, tarfile, uuid

BROKER, work, job_user = sys.argv[1:4]

def ask(req, body=b""):
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(60)
        c.connect(BROKER)
        c.sendall(json.dumps(req).encode() + b"\n" + body)
        buf = b""
        while b"\n" not in buf:
            got = c.recv(65536)
            if not got:
                break
            buf += got
    return json.loads(buf)

def say(ok, what):
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)

def archive():
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        for name in ("probe.py", "memory.py", "refused.txt", "sockets.txt", "proxy-ports.txt", "own-ip.txt", "expect.json"):
            t.add(os.path.join(work, name), arcname=name)
    return b.getvalue()

def run(argv, timeout=120):
    """One job, start to end: (its output, its exit)."""
    turn = str(uuid.uuid4())
    data = archive()
    got = ask({"op": "export", "turn": turn, "size": len(data)}, data)
    if not got.get("ok"):
        say(False, f"export was refused: {got}")
        return "", {}
    got = ask({"op": "write_token", "turn": turn, "provider": "self-check", "size": 2}, b"{}")
    say(bool(got.get("ok")), "the broker writes a token copy")
    output, exit_, buf = b"", {}, b""
    with socket.socket(socket.AF_UNIX) as c:
        c.settimeout(timeout + 60)
        c.connect(BROKER)
        c.sendall(json.dumps({"op": "start", "turn": turn, "slug": "self-check", "run": argv,
                              "timeout_seconds": timeout}).encode() + b"\n")
        while not exit_:
            got = c.recv(65536)
            if not got:
                break
            buf += got
            while len(buf) >= 5 and len(buf) >= 5 + struct.unpack(">I", buf[1:5])[0]:
                n = struct.unpack(">I", buf[1:5])[0]
                kind, payload, buf = buf[:1], buf[5:5 + n], buf[5 + n:]
                if kind == b"O":
                    sys.stdout.write(payload.decode("utf-8", "replace"))
                    sys.stdout.flush()
                    output += payload
                else:
                    exit_ = json.loads(payload)
    return output.decode("utf-8", "replace"), exit_

status = ask({"op": "status"})
say(status.get("ok") is True, f"the broker answers status: {status}")
say(status.get("supported") is True, f"its systemd can run the unit ({status.get('systemd')})")
json.dump({"locked": bool(status.get("locked"))}, open(os.path.join(work, "expect.json"), "w"))
if status.get("locked"):
    print("SKIP  a runner on this box is locked: the broker starts no job here, and no job is reported")
    sys.exit(0)

for req in ({"op": "exec", "command": "id"}, {"op": "start", "turn": "../x", "slug": "a", "run": ["x"], "timeout_seconds": 1},
            {"op": "start", "turn": str(uuid.uuid4()), "slug": "a", "run": "id", "timeout_seconds": 1},
            {"op": "status", "user": "root"}, {"op": "export", "turn": str(uuid.uuid4()), "size": 1, "path": "/etc/x"}):
    got = ask(req)
    say("error" in got, f"the broker refuses {json.dumps(req)[:70]}")

ask({"op": "cleanup"})
output, exit_ = run(["python3", "probe.py"])
say("PROBE-DONE" in output, "the probe ran to its end")
say(exit_ == {"rc": 0, "timed_out": False}, f"a good job exits 0 ({exit_})")
left = subprocess.run(["pgrep", "-u", job_user], capture_output=True).stdout.split()
say(not left, f"nothing of {job_user} outlives the job, a process in a new session included ({len(left)})")
ask({"op": "cleanup"})

output, exit_ = run(["python3", "memory.py"])
say("ALLOCATED" not in output and exit_.get("rc") not in (0, None), f"MemoryMax ended a 3 GiB allocation ({exit_})")
ask({"op": "cleanup"})

output, exit_ = run(["python3", "-c", "import signal,time; signal.signal(15, signal.SIG_IGN); time.sleep(60)"], timeout=3)
say(exit_.get("timed_out") is True, f"RuntimeMaxSec ends a job that ignores TERM, and reads as a timeout ({exit_})")
got = ask({"op": "cleanup"})
say(bool(got.get("ok")), "cleanup works")
PY
chmod 644 "$WORK"/*
# The client, as the runner, says what status said here for the probe to read.
: > "$WORK/expect.json"
chmod 666 "$WORK/expect.json"

echo "== a real unit, through the broker, as $first_user"
out="$(cd / && sudo -n -u "$first_user" python3 "$WORK/client.py" "$BROKER" "$WORK" "$JOB_USER" 2>&1)"
printf '%s\n' "$out" | grep -v '^PROBE-DONE$'
FAILS=$((FAILS + $(grep -c '^FAIL' <<<"$out")))
if ! grep -q '^PASS' <<<"$out"; then
  fail "the client didn't run"
fi
left="$(find "$JOBS_ROOT" -mindepth 1 2>/dev/null | wc -l)"
if [ "$left" = 0 ]; then
  pass "cleanup empties $JOBS_ROOT"
else
  fail "cleanup left $left entries in $JOBS_ROOT"
fi

echo "== other users at the broker"
ask_as='
import json, socket, sys
s = socket.socket(socket.AF_UNIX)
s.settimeout(5)
s.connect(sys.argv[1])
s.sendall(b"{\"op\": \"status\", \"role\": \"labs-observe\", \"agent\": \"eva\"}\n")
print(s.recv(4096).decode("utf-8", "replace"))
'
for sock in "$BROKER" /run/canopy-kms/sock /run/canopy-aws/sock; do
  if [ ! -S "$sock" ]; then
    skip "$sock isn't on this box"
    continue
  fi
  said="$(sudo -n -u "$JOB_USER" python3 -c "$ask_as" "$sock" 2>&1)"
  case "$said" in
    *"not a runner"*) pass "$sock refuses $JOB_USER" ;;
    *) fail "$sock said something other than a refusal to $JOB_USER: $said" ;;
  esac
done
said="$(python3 -c "$ask_as" "$BROKER" 2>&1)"
case "$said" in
  *"not a runner"*) pass "$BROKER refuses root" ;;
  *) fail "$BROKER said something other than a refusal to root: $said" ;;
esac

echo
if [ "$FAILS" -gt 0 ]; then
  echo "$FAILS check(s) FAILED."
  exit 1
fi
echo "All checks passed."
