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
LISTENER_PID=""
FAILS=0
#: What the client and the probe each report whatever the box, on top of one line per entry in
#: refused.txt, sockets.txt and closed-targets.txt. Fewer verdicts than that means something
#: crashed part-way, however many PASSes came before it. (A test runs both against these.)
CLIENT_CHECKS=17
PROBE_CHECKS=19

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
skip() { printf 'SKIP  %s\n' "$1"; }

cleanup() {
  [ -n "$LISTENER_PID" ] && kill "$LISTENER_PID" 2>/dev/null
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

echo "== what a job should be refused has to be there first"
# A refused connection only proves anything if something was listening. So: a real listener, as
# root, on loopback and the box's own address, reached from the root side. Whatever can't be
# reached from here is a FAIL, never a job's PASS.
LOOP_PORT=48731
listen_py='
import select, socket, sys
socks = []
for spec in sys.argv[1:]:
    host, port = spec.rsplit(",", 1)
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, int(port)))
    except OSError:
        continue   # the proxy on its own port; or no IPv6. The root-side reach below decides.
    s.listen(8)
    socks.append(s)
while True:
    for s in select.select(socks, [], [])[0]:
        c, _ = s.accept()
        c.close()
'
reach_py='
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
s.settimeout(4)
s.connect((host, port))
'
listen_on=("0.0.0.0,$LOOP_PORT" "::1,$LOOP_PORT")
while read -r id _user _home uid; do
  [ -n "${id:-}" ] || continue
  listen_on+=("127.0.0.1,$((20000 + uid % 10000))")
done <<<"$rows"
python3 -c "$listen_py" "${listen_on[@]}" & LISTENER_PID=$!
sleep 1
: > "$WORK/closed-targets.txt"
target() {  # <what> <host> <port>: a job must be refused this, once root has reached it
  if python3 -c "$reach_py" "$2" "$3" 2>/dev/null; then
    pass "root reaches $1 ($2:$3), so a job's refusal means something"
    printf '%s|%s|%s\n' "$1" "$2" "$3" >> "$WORK/closed-targets.txt"
  else
    fail "can't establish that $1 ($2:$3) is reachable from root, so a job's refusal would prove nothing"
  fi
}
target "IMDSv4" 169.254.169.254 80
target "localhost" 127.0.0.1 "$LOOP_PORT"
target "the rest of loopback" 127.0.0.2 "$LOOP_PORT"
target "the DNS stub's other ports" 127.0.0.53 "$LOOP_PORT"
own_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$own_ip" ]; then
  target "the box's own address" "$own_ip" "$LOOP_PORT"
else
  fail "can't establish the box's own address"
fi
# IPv6 may truly not be here, and then there is nothing for a job to reach.
if python3 -c "$reach_py" ::1 "$LOOP_PORT" 2>/dev/null; then
  target "IPv6 localhost" ::1 "$LOOP_PORT"
else
  skip "IPv6 localhost: this box has none"
fi
if python3 -c "$reach_py" fd00:ec2::254 80 2>/dev/null; then
  target "IMDSv6" fd00:ec2::254 80
else
  skip "IMDSv6: not answering even for root here"
fi
while read -r id _user _home uid; do
  [ -n "${id:-}" ] || continue
  target "$id's egress proxy port" 127.0.0.1 "$((20000 + uid % 10000))"
done <<<"$rows"

# A sibling's folder, planted after a clean slot: the unit has to see only its own.
sudo -n -u "$first_user" python3 -c '
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(60); s.connect(sys.argv[1])
s.sendall(b"{\"op\": \"cleanup\"}\n"); print(s.recv(4096).decode())' "$BROKER" >/dev/null
sudo -n -u "$JOB_USER" sh -c "mkdir -p $JOBS_ROOT/sibling-check && echo a-sibling-token > $JOBS_ROOT/sibling-check/secret"
if [ "$(cat "$JOBS_ROOT/sibling-check/secret" 2>/dev/null)" = a-sibling-token ]; then
  pass "root sees a sibling's token file, so a job's not seeing it means something"
else
  fail "can't establish a sibling's token file at $JOBS_ROOT/sibling-check/secret"
fi

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
try:
    mode = os.stat(tokens).st_mode & 0o777
    (pass_ if mode == 0o600 else fail)(f"its token copy is 0600 ({oct(mode)})")
except OSError as exc:
    fail(f"its token copy isn't there: {exc}")

# Each unit sees only its own job: the sibling planted before this ran, and nothing else.
root = os.path.dirname(os.path.dirname(os.environ["CANOPY_JOB_TOKENS"]))
sibling = root + "/sibling-check/secret"
try:
    with open(sibling, "rb"):
        fail(f"a job can read a sibling's token file ({sibling})")
except OSError as exc:
    pass_(f"a job can't read a sibling's token file ({errno.errorcode.get(exc.errno, exc.errno)})")
try:
    names = os.listdir(root)
    (pass_ if names == [os.environ["CANOPY_TURN_ID"]] else fail)(f"a job sees only its own folder in {root} ({len(names)})")
except OSError as exc:
    fail(f"could not list {root}: {exc}")

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
    """Root reached each of these a moment ago: not connecting is the refusal, and a unit's
    IPAddressDeny drops rather than rejects, so silence counts as much as a reset."""
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    s.settimeout(4)
    try:
        s.connect((host, port))
        fail(f"a job reached {what} ({host}:{port})")
    except OSError as exc:
        pass_(f"a job is refused {what} ({host}:{port}, {errno.errorcode.get(exc.errno, 'no answer')})")

for row in lines("closed-targets.txt"):
    what, host, port = row.split("|")
    closed(what, host, int(port))

try:
    socket.getaddrinfo("canopy.dimagi.com", 443)
    pass_("DNS works for a job (getaddrinfo canopy.dimagi.com)")
except OSError as exc:
    fail(f"DNS doesn't work for a job: {exc}")

# Other users' processes. ProtectProc=invisible hides them entirely.
others = []
try:
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                if os.stat(f"/proc/{name}").st_uid != os.geteuid():
                    others.append(name)
            except OSError:
                pass
    (pass_ if not others else fail)(f"a job sees no other user's process in /proc ({len(others)})")
except OSError as exc:
    fail(f"could not list /proc: {exc}")

# The limits, as the cgroup itself states them.
for name, value in (("memory.max", str(2 * 1024 ** 3)), ("memory.swap.max", "0"), ("pids.max", "256")):
    try:
        path = open("/proc/self/cgroup").read().strip().split("::")[-1]
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
EXTRA = os.environ.get("SELF_CHECK_EXTRA", "").split()   # words on each run, which a test finds them by

def ask(req, body=b"", keep=False):
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(60)
    c.connect(BROKER)
    try:
        c.sendall(json.dumps(req).encode() + b"\n" + body)
    except OSError:
        pass   # a refused export closes without reading its body; the reply still says why
    buf = b""
    while b"\n" not in buf:
        got = c.recv(65536)
        if not got:
            break
        buf += got
    if keep:
        return json.loads(buf), c
    c.close()
    return json.loads(buf)

def say(ok, what):
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)

def archive():
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        for name in ("probe.py", "memory.py", "refused.txt", "sockets.txt", "closed-targets.txt"):
            t.add(os.path.join(work, name), arcname=name)
    return b.getvalue()

class Job:
    """One slot, from export to `done`: the connection stays open, as a runner's does."""
    def __init__(self):
        self.turn = str(uuid.uuid4())
        data = archive()
        got, self.hold = ask({"op": "export", "turn": self.turn, "size": len(data)}, data, keep=True)
        if got.get("ok"):
            got = ask({"op": "write_token", "turn": self.turn, "provider": "self-check", "size": 2}, b"{}")
        say(bool(got.get("ok")), f"the broker takes an export and writes a token copy ({got})")

    def run(self, argv, timeout=120):
        output, exit_, buf = b"", {}, b""
        with socket.socket(socket.AF_UNIX) as c:
            c.settimeout(timeout + 60)
            c.connect(BROKER)
            c.sendall(json.dumps({"op": "start", "turn": self.turn, "slug": "self-check", "run": [*argv, *EXTRA],
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

    def done(self):
        got = ask({"op": "cleanup"})
        self.hold.close()
        return got

status = ask({"op": "status"})
say(status.get("ok") is True, f"the broker answers status: {status}")
say(status.get("supported") is True, f"its systemd can run the unit ({status.get('systemd')})")
if status.get("locked"):
    print("SKIP  a runner on this box is locked: the broker starts no job here, and no job is reported")
    sys.exit(3)

for req in ({"op": "exec", "command": "id"}, {"op": "start", "turn": "../x", "slug": "a", "run": ["x"], "timeout_seconds": 1},
            {"op": "start", "turn": str(uuid.uuid4()), "slug": "a", "run": "id", "timeout_seconds": 1},
            {"op": "status", "user": "root"}, {"op": "export", "turn": str(uuid.uuid4()), "size": 1, "path": "/etc/x"}):
    say("error" in ask(req), f"the broker refuses {json.dumps(req)[:70]}")

job = Job()
output, exit_ = job.run(["python3", "probe.py"])
say("PROBE-DONE" in output, "the probe ran to its end")
say(exit_ == {"rc": 0, "timed_out": False}, f"a good job exits 0 ({exit_})")
left = subprocess.run(["pgrep", "-u", job_user], capture_output=True).stdout.split()
say(not left, f"nothing of {job_user} outlives the job, a process in a new session included ({len(left)})")
data = archive()
got = ask({"op": "export", "turn": str(uuid.uuid4()), "size": len(data)}, data)
say("Another job" in str(got.get("error")), f"the job has exited but its slot holds: a second export is refused ({got})")
job.done()

job = Job()
output, exit_ = job.run(["python3", "memory.py"])
say("ALLOCATED" not in output and exit_.get("rc") not in (0, None), f"MemoryMax ended a 3 GiB allocation ({exit_})")
job.done()

job = Job()
output, exit_ = job.run(["python3", "-c", "import signal,time; signal.signal(15, signal.SIG_IGN); time.sleep(60)"], timeout=3)
say(exit_.get("timed_out") is True, f"RuntimeMaxSec ends a job that ignores TERM, and reads as a timeout ({exit_})")
got = job.done()
say(bool(got.get("ok")), f"cleanup works ({got})")
PY
chmod 644 "$WORK"/*

echo "== a real unit, through the broker, as $first_user"
out="$(cd / && sudo -n -u "$first_user" python3 "$WORK/client.py" "$BROKER" "$WORK" "$JOB_USER" 2>&1)"
rc=$?
printf '%s\n' "$out" | grep -v '^PROBE-DONE$'
FAILS=$((FAILS + $(grep -c '^FAIL' <<<"$out")))
said="$(grep -cE '^(PASS|FAIL)' <<<"$out")"
if [ "$rc" = 3 ]; then
  # A locked box: the broker runs nothing, so all that's asked is that it says so.
  expected=2
  rc=0
else
  expected=$((CLIENT_CHECKS + PROBE_CHECKS + $(wc -l < "$WORK/refused.txt") + $(wc -l < "$WORK/sockets.txt") \
    + $(wc -l < "$WORK/closed-targets.txt")))
fi
if [ "$rc" != 0 ]; then
  fail "the client exited $rc, so what it didn't get to wasn't checked"
fi
if [ "$said" = "$expected" ]; then
  pass "every check reported ($said of $expected expected)"
else
  fail "only $said of $expected expected checks reported, so the rest weren't checked"
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
