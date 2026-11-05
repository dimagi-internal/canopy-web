"""The job broker on a real Unix socket, with its three entry points to the box stubbed.

What stands in: `spawn`, `run_unit` and `run_unit_pipe` each take the argv `confined_argv` built,
insist it is `systemd-run`, and run what follows its `--` as this user in the `WorkingDirectory=`
it names; `stop` stands for `systemctl stop` of a unit, killing everything carrying this test's
marker as `KillMode=control-group` would, and `no_processes` for `pkill` + `pgrep` of the uid. So
what these prove is the broker's and the runner's half. `job_self_check.sh` proves systemd's, on a
box.

The marker is looked for here, in Python, and never passed to a process: a `pkill -f <marker>` has
the marker on its own command line, so it would SIGKILL a concurrent `pgrep -f <marker>` (read as
"nothing left"), and that `pgrep` would match the `pkill` (read as a survivor).
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

EC2 = pathlib.Path(__file__).resolve().parent.parent


def load_broker():
    spec = importlib.util.spec_from_file_location("job_broker", EC2 / "job_broker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def carrying(marker) -> list:
    """The pids whose command line has `marker` in it. A zombie's has gone."""
    listing = subprocess.run(["ps", "-A", "-ww", "-o", "pid=", "-o", "command="], capture_output=True, text=True)
    found = []
    for line in listing.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if marker in command:
            found.append(int(pid))
    return found


def _props(argv):
    return dict(a.removeprefix("--property=").split("=", 1) for a in argv if a.startswith("--property="))


class Harness:
    def __init__(self, mod, root, marker, *, uid=2001, runners=(2001,), locked=False):
        self.mod, self.marker = mod, marker
        self.uid, self.runners, self.locked = uid, set(runners), locked
        self.requests, self.spawned, self.stops, self.as_job_calls, self.procs = [], [], [], [], []
        self.dir = tempfile.mkdtemp(prefix="jb", dir="/tmp")
        self.path = os.path.join(self.dir, "sock")
        harness = self

        class Recording(mod.JobBroker):
            def dispatch(self, req, rest, conn, uid):
                harness.requests.append((req["op"], uid))
                return super().dispatch(req, rest, conn, uid)

        # Beside the system's, so that `python3` is found under `env -i` here as it is on a box.
        mod.JOB_PATH = f"{pathlib.Path(sys.executable).parent}:/usr/bin:/bin"
        self.survivors, self.unit_alive, self.stop_ok, self.sweeps = False, False, True, []
        self.chowned, self.helper_units, self.pgrep_calls = [], [], 0
        self.server = Recording(self.path, root=pathlib.Path(root), run_unit=self.run_unit,
                                run_unit_pipe=self.run_unit_pipe, spawn=self.spawn, stop=self.stop,
                                stop_all=lambda: self.sweeps.append("stop_all"), no_processes=self.no_processes,
                                unit_state=self.unit_state, chown_fd=lambda fd: self.chowned.append(fd),
                                uid_of=lambda sock: self.uid,
                                runner_uids=lambda: self.runners,
                                box_state=lambda: {"locked": self.locked, "systemd": 255, "job_user": True})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _tail(self, argv):
        assert argv[0] == "systemd-run", argv
        self.helper_units.append(list(argv))
        props = _props(argv)
        tail = argv[argv.index("--") + 1:]
        self.as_job_calls.append(list(tail))
        return tail, props["WorkingDirectory"]

    def run_unit(self, argv, input=None):
        tail, cwd = self._tail(argv)
        return subprocess.run(tail, input=input, capture_output=True, timeout=60, cwd=cwd)

    def run_unit_pipe(self, argv):
        tail, cwd = self._tail(argv)
        return subprocess.Popen(tail, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, cwd=cwd)

    def no_processes(self):
        self.pgrep_calls += 1
        self.kill_all()
        return not self.survivors

    def unit_state(self, unit):
        return "active" if self.unit_alive else "inactive"

    def spawn(self, argv):
        self.spawned.append(list(argv))
        assert argv[0] == "systemd-run", argv
        props = _props(argv)
        command = argv[argv.index("--") + 1:]
        proc = subprocess.Popen(command, cwd=props["WorkingDirectory"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(proc)
        timer = threading.Timer(float(props["RuntimeMaxSec"]), self.kill_all)
        timer.daemon = True
        timer.start()
        threading.Thread(target=lambda: (proc.wait(), timer.cancel()), daemon=True).start()
        return proc

    def stop(self, turn):
        self.stops.append(turn)
        self.kill_all()
        return self.stop_ok

    def kill_all(self):
        """Until none is left, as `no_processes_left` does: a killed process lingers a moment."""
        deadline = time.monotonic() + 5
        while (pids := carrying(self.marker)) and time.monotonic() < deadline:
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.01)
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()

    def close(self):
        self.kill_all()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)
