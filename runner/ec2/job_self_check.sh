#!/usr/bin/env bash
# Prove, on a real box, what the runner's tests can't: that `canopy-job` is refused
# everything a plain job must not reach. Run once as root, after the box's setup has run:
#
#   sudo bash /opt/canopy-web/runner/ec2/job_self_check.sh
#
# It starts nothing of the runners', reads no secret's content (a refused read is the
# pass) and leaves nothing behind. But `pkill -u canopy-job` below would take a running
# job, so it stops at once if a job holds the box's lock. One PASS or FAIL per line, SKIP
# where the box doesn't have the thing. Exit 0 only with no FAIL.
set -uo pipefail

JOB_USER=canopy-job
JOBS_ROOT=/var/lib/canopy-jobs
SUDOERS=/etc/sudoers.d/canopy-jobs
LOCK=/run/canopy/job.lock
RUNNERS_FILE=/etc/canopy-runner/runners.json
ETC=/etc/canopy-runner
PROBE_DIR=""
LISTENER_PID=""
FAILS=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILS=$((FAILS + 1)); }
skip() { printf 'SKIP  %s\n' "$1"; }
as_job() { sudo -n -u "$JOB_USER" "$@"; }

cleanup() {
  [ -n "$LISTENER_PID" ] && kill "$LISTENER_PID" 2>/dev/null
  [ -n "$PROBE_DIR" ] && rm -rf "$PROBE_DIR"
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

# Exit 0 only if connecting is refused or unreachable; a connection or a silent drop isn't.
refused_py='
import errno, socket, sys
host, port = sys.argv[1], int(sys.argv[2])
fam = socket.AF_INET6 if ":" in host else socket.AF_INET
s = socket.socket(fam)
s.settimeout(4)
try:
    s.connect((host, port))
except OSError as e:
    sys.exit(0 if e.errno in (errno.ECONNREFUSED, errno.EACCES, errno.EPERM, errno.ENETUNREACH,
                              errno.EHOSTUNREACH) else 1)
sys.exit(1)
'
expect_refused() {  # <what> <host> <port>
  if as_job python3 -c "$refused_py" "$2" "$3"; then
    pass "$1 is refused ($2:$3)"
  else
    fail "$1 was reachable or gave no refusal ($2:$3)"
  fi
}
expect_unreadable() {  # <path>: neither readable nor listable by the job user
  if [ ! -e "$1" ]; then
    skip "$1 isn't on this box"
  elif as_job test -r "$1" 2>/dev/null; then
    fail "$JOB_USER can read $1"
  else
    pass "$JOB_USER can't read $1"
  fi
}

echo "== the user"
shell="$(getent passwd "$JOB_USER" | cut -d: -f7)"
case "$shell" in
  */nologin|*/false) pass "$JOB_USER has no login shell ($shell)" ;;
  *) fail "$JOB_USER has a login shell: $shell" ;;
esac
job_uid="$(id -u "$JOB_USER")"
if [ "$job_uid" -lt 1000 ] && [ "$job_uid" -gt 0 ]; then
  pass "$JOB_USER is a system user (uid $job_uid)"
else
  fail "$JOB_USER has uid $job_uid: expected a system user"
fi
groups_of="$(id -Gn "$JOB_USER")"
if [ "$groups_of" = "$JOB_USER" ] || [ "$(wc -w <<<"$groups_of")" = 1 ]; then
  pass "$JOB_USER is in no other group ($groups_of)"
else
  fail "$JOB_USER is in other groups: $groups_of"
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

echo "== sudo"
if visudo -c >/dev/null 2>&1; then pass "visudo -c accepts the whole sudoers"; else fail "visudo -c refuses the sudoers"; fi
if [ "$(stat -c '%U %a' "$SUDOERS" 2>/dev/null)" = "root 440" ]; then
  pass "$SUDOERS is root's, 0440"
else
  fail "$SUDOERS should be root's and 0440: $(stat -c '%U %a' "$SUDOERS" 2>&1)"
fi
if grep -vE '^(#.*|[a-z0-9-]+ ALL=\(canopy-job\) NOPASSWD: ALL|)$' "$SUDOERS" >/dev/null 2>&1; then
  fail "$SUDOERS holds a line that isn't a runner-as-$JOB_USER rule"
else
  pass "$SUDOERS holds only runner-as-$JOB_USER rules"
fi
if as_job sudo -n true >/dev/null 2>&1; then
  fail "$JOB_USER can use sudo"
else
  pass "$JOB_USER can't use sudo"
fi
if as_job su -c true root </dev/null >/dev/null 2>&1; then
  fail "$JOB_USER can become root with su"
else
  pass "$JOB_USER can't become root with su"
fi

echo "== each runner"
rows="$(python3 - "$RUNNERS_FILE" <<'PY' 2>/dev/null
import json, sys
for r in json.load(open(sys.argv[1])).get("runners") or []:
    print(r["id"], r["user"], r["home"], r["uid"])
PY
)"
if [ -z "$rows" ]; then
  fail "no runners in $RUNNERS_FILE"
fi
while read -r id user home uid; do
  [ -n "${id:-}" ] || continue
  # What the runner's own units say: sudo can't work at all under NoNewPrivileges.
  nnp="$(systemctl show -p NoNewPrivileges --value "canopy-runner@$id.service" 2>/dev/null)"
  if [ "$nnp" = "yes" ]; then
    fail "canopy-runner@$id has NoNewPrivileges=yes, so it can't sudo to $JOB_USER and reports no jobs"
  else
    pass "canopy-runner@$id isn't under NoNewPrivileges"
  fi
  if sudo -n -u "$user" sudo -n -u "$JOB_USER" true 2>/dev/null; then
    pass "$user can run as $JOB_USER"
  else
    fail "$user can't run as $JOB_USER"
  fi
  if sudo -n -u "$user" sudo -n -u root true 2>/dev/null; then
    fail "$user can run as root"
  else
    pass "$user can't run as root"
  fi
  if sudo -n -u "$user" python3 -c 'import yaml' 2>/dev/null; then
    pass "$user has PyYAML"
  else
    fail "$user can't import yaml"
  fi
  for path in "$home" "$home/.canopy-runner" "$home/.canopy-runner/runner-token.json" \
      "$home/.canopy" "$home/.canopy/connections" "$home/.canopy/device-key" \
      "$home/.canopy-cloud-runner.json" "$home/work" "$home/agents" "$home/canopy-web" "$home/.claude" \
      "$ETC/$id.env" "$ETC/$id.gog-secret" "/run/canopy/$uid"; do
    expect_unreadable "$path"
  done
  # A home's own mode is what shields all of it: check it directly as well.
  if [ "$(stat -c '%a' "$home" 2>/dev/null)" = 700 ]; then
    pass "$home is 0700"
  else
    fail "$home should be 0700: $(stat -c '%a' "$home" 2>&1)"
  fi
  # Every agent's own `~/.<slug>/.env`, as root sees them, then as the job user can't.
  for envfile in "$home"/.*/.env; do
    [ -e "$envfile" ] && expect_unreadable "$envfile"
  done
done <<<"$rows"
for path in "$ETC/box.env" "$ETC/egress.state.json" /root /etc/shadow /etc/sudoers \
    /opt/canopy-runner/runner.env /opt/canopy-runner/runner-token.json; do
  expect_unreadable "$path"
done

echo "== everything $JOB_USER can read in the places secrets live"
readable="$(as_job find "$ETC" /run/canopy /run/canopy-kms /run/canopy-aws /home /root \
    -xdev -type f -readable 2>/dev/null | grep -vE "^$ETC/(runners\.json|egress\.json|[^/]+\.egress\.allow)$")"
if [ -z "$readable" ]; then
  pass "nothing unexpected is readable under $ETC, /run/canopy*, /home or /root"
else
  fail "$JOB_USER can read: $(tr '\n' ' ' <<<"$readable")"
fi

echo "== the metadata address"
expect_refused "IMDSv4" 169.254.169.254 80
if command -v ip6tables >/dev/null 2>&1 && [ -e /proc/net/if_inet6 ]; then
  expect_refused "IMDSv6" fd00:ec2::254 80
else
  skip "IMDSv6: no IPv6 on this box"
fi
if iptables -S CANOPY-JOB 2>/dev/null | grep -qF -- "--uid-owner $job_uid"; then
  pass "CANOPY-JOB holds rules for uid $job_uid"
else
  fail "CANOPY-JOB holds no rule for uid $job_uid"
fi
if iptables -S OUTPUT 2>/dev/null | grep -qF -- "-j CANOPY-JOB"; then
  pass "OUTPUT jumps to CANOPY-JOB"
else
  fail "OUTPUT doesn't jump to CANOPY-JOB"
fi
if grep -q 'canopy-runners.service' <(systemctl list-unit-files canopy-runners.service 2>/dev/null) \
    && [ "$(systemctl is-enabled canopy-runners.service 2>/dev/null)" = enabled ]; then
  pass "canopy-runners.service runs every boot, so the rules come back after a reboot"
else
  fail "canopy-runners.service isn't enabled, so the rules won't survive a reboot"
fi

echo "== loopback"
listen_py='
import socket, sys
s = socket.socket(socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((sys.argv[1], int(sys.argv[2])))
s.listen(8)
while True:
    c, _ = s.accept()
    c.close()
'
LOOP_PORT=48731
python3 -c "$listen_py" 0.0.0.0 "$LOOP_PORT" & LISTENER_PID=$!
sleep 1
expect_refused "A service on loopback" 127.0.0.1 "$LOOP_PORT"
expect_refused "A service on 127.0.0.2" 127.0.0.2 "$LOOP_PORT"
own_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$own_ip" ]; then
  expect_refused "A service on the box's own address" "$own_ip" "$LOOP_PORT"
fi
kill "$LISTENER_PID" 2>/dev/null; LISTENER_PID=""
if [ -e /proc/net/if_inet6 ]; then
  python3 -c "$listen_py" ::1 "$LOOP_PORT" 2>/dev/null & LISTENER_PID=$!
  sleep 1
  expect_refused "A service on IPv6 loopback" ::1 "$LOOP_PORT"
  kill "$LISTENER_PID" 2>/dev/null; LISTENER_PID=""
fi
while read -r id user home uid; do
  [ -n "${id:-}" ] || continue
  expect_refused "$id's egress proxy port" 127.0.0.1 "$((20000 + uid % 10000))"
done <<<"$rows"

echo "== the egress lock, and DNS"
# A lock on any runner closes the job user off entirely, DNS with it.
# (`iptables -S` prints `--reject-with ...` after it, and -o/-d before the owner match on
# the other rules: only the lock's rule starts with the owner match.)
locked="$(iptables -S CANOPY-JOB 2>/dev/null | grep -cE -- "^-A CANOPY-JOB -m owner --uid-owner $job_uid -j REJECT")"
if [ "${locked:-0}" -gt 0 ]; then
  skip "DNS: a runner on this box is locked, so $JOB_USER is closed off entirely"
  expect_refused "A public address under the lock" 1.1.1.1 443
elif as_job getent hosts canopy.dimagi.com >/dev/null 2>&1; then
  pass "DNS works for $JOB_USER (getent hosts canopy.dimagi.com)"
else
  fail "DNS doesn't work for $JOB_USER"
fi

echo "== the brokers"
broker_py='
import socket, sys
s = socket.socket(socket.AF_UNIX)
s.settimeout(5)
s.connect(sys.argv[1])
s.sendall(b"{\"op\": \"public_key\", \"role\": \"labs-observe\", \"agent\": \"eva\"}\n")
print(s.recv(4096).decode("utf-8", "replace"))
'
for sock in /run/canopy-kms/sock /run/canopy-aws/sock; do
  if [ ! -S "$sock" ]; then
    skip "$sock isn't on this box"
    continue
  fi
  said="$(as_job python3 -c "$broker_py" "$sock" 2>&1)"
  case "$said" in
    *"not a runner"*) pass "$sock refuses $JOB_USER" ;;
    *) fail "$sock said something other than a refusal to $JOB_USER: $said" ;;
  esac
done

echo "== other runners' processes"
first_uid="$(awk 'NR==1 {print $4}' <<<"$rows")"
pid="$(pgrep -u "$first_uid" -o python3 2>/dev/null || true)"
if [ -n "$pid" ]; then
  if as_job test -r "/proc/$pid/environ"; then
    fail "$JOB_USER can read /proc/$pid/environ of a runner"
  else
    pass "$JOB_USER can't read a runner's environment (/proc/$pid/environ)"
  fi
else
  skip "no runner process to try /proc against"
fi
# `hidepid` isn't set by this setup, so any local user sees other users' command lines.
# Nothing should carry a secret there: this looks, and says what it finds.
leaky="$(as_job sh -c 'cat /proc/[0-9]*/cmdline 2>/dev/null | tr "\0" " "' \
    | grep -oE '(gh[pousr]_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{10,}|crt_[A-Za-z0-9_-]{10,}|cct_[A-Za-z0-9_-]{10,})' \
    | cut -c1-8 | sort -u | tr '\n' ' ')"
if [ -z "$leaky" ]; then
  pass "no token-shaped value in any process's command line right now"
else
  fail "a token-shaped value is in a command line, starting: $leaky"
fi

echo "== what could outlive a job"
if as_job sh -c 'echo "* * * * * true" | crontab -' >/dev/null 2>&1; then
  fail "$JOB_USER can install a crontab"
  as_job crontab -r 2>/dev/null
else
  pass "$JOB_USER can't install a crontab"
fi
if command -v at >/dev/null 2>&1; then
  if as_job sh -c 'echo true | at now + 1 hour' >/dev/null 2>&1; then
    fail "$JOB_USER can queue an at job"
  else
    pass "$JOB_USER can't queue an at job"
  fi
else
  skip "at isn't installed"
fi
as_job sh -c 'nohup sleep 300 >/dev/null 2>&1 &'
as_job setsid sleep 300 >/dev/null 2>&1 &
sleep 1
as_job pkill -KILL -u "$JOB_USER"
sleep 1
if pgrep -u "$JOB_USER" >/dev/null; then
  fail "pkill -KILL -u $JOB_USER left processes running"
else
  pass "pkill -KILL -u $JOB_USER leaves nothing, a background process and a new session included"
fi

echo "== a run, the way the runner starts one"
PROBE_DIR="$(as_job mktemp -d -p "$JOBS_ROOT" self-check.XXXXXX)"
six="PATH=/usr/local/bin:/usr/bin:/bin HOME=$PROBE_DIR LANG=C.UTF-8 CANOPY_AGENT_SLUG=self-check CANOPY_TURN_ID=self-check CANOPY_JOB_TOKENS=$PROBE_DIR"
# shellcheck disable=SC2086 # the six words are meant to split
got="$(as_job env -i -C "$PROBE_DIR" $six env | cut -d= -f1 | sort | tr '\n' ' ')"
if [ "$got" = "CANOPY_AGENT_SLUG CANOPY_JOB_TOKENS CANOPY_TURN_ID HOME LANG PATH " ]; then
  pass "a job's environment is exactly the six"
else
  fail "a job's environment holds: $got"
fi
# shellcheck disable=SC2086
if [ "$(as_job env -i -C "$PROBE_DIR" $six pwd)" = "$PROBE_DIR" ]; then
  pass "a job runs in the folder it was given"
else
  fail "a job didn't start in its folder"
fi
token_file="$PROBE_DIR/t.json"
# shellcheck disable=SC2016 # the inner shell expands them
printf 'x' | as_job sh -c 'umask 077; cat > "$1.tmp" && mv -f "$1.tmp" "$1"' sh "$token_file"
if [ "$(stat -c '%U %a' "$token_file" 2>/dev/null)" = "$JOB_USER 600" ]; then
  pass "a token copy is $JOB_USER's, 0600"
else
  fail "a token copy should be $JOB_USER's and 0600: $(stat -c '%U %a' "$token_file" 2>&1)"
fi

echo
if [ "$FAILS" -gt 0 ]; then
  echo "$FAILS check(s) FAILED."
  exit 1
fi
echo "All checks passed."
