"""The job broker on a real Unix socket, with its three doors to the box stubbed.

What stands in: `as_job` runs as this user rather than `canopy-job`; `spawn` reads the
argv `systemd_run_argv` built and runs what follows its `--` in the `WorkingDirectory=` it
names, enforcing `RuntimeMaxSec=`; `stop` stands for `systemctl stop` of a unit, killing
everything carrying this test's marker as `KillMode=control-group` would. So what these prove
is the broker's and the runner's half. `job_self_check.sh` proves systemd's, on a box.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading

EC2 = pathlib.Path(__file__).resolve().parent.parent


def load_broker():
    spec = importlib.util.spec_from_file_location("job_broker", EC2 / "job_broker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Harness:
    def __init__(self, mod, root, marker, *, uid=2001, runners=(2001,), locked=False):
        self.mod, self.marker = mod, marker
        self.uid, self.runners, self.locked = uid, set(runners), locked
        self.requests, self.spawned, self.stops, self.as_job_calls = [], [], [], []
        self.dir = tempfile.mkdtemp(prefix="jb", dir="/tmp")
        self.path = os.path.join(self.dir, "sock")
        harness = self

        class Recording(mod.JobBroker):
            def dispatch(self, req, body, conn, uid):
                harness.requests.append((req["op"], uid))
                return super().dispatch(req, body, conn, uid)

        self.server = Recording(self.path, root=pathlib.Path(root), as_job=self.as_job, spawn=self.spawn,
                                stop=self.stop, uid_of=lambda sock: self.uid,
                                runner_uids=lambda: self.runners,
                                box_state=lambda: {"locked": self.locked, "systemd": 255, "job_user": True})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def as_job(self, args, input=None):
        self.as_job_calls.append(list(args))
        return subprocess.run(list(args), input=input, capture_output=True, timeout=60)

    def spawn(self, argv):
        self.spawned.append(list(argv))
        props = dict(a.removeprefix("--property=").split("=", 1) for a in argv if a.startswith("--property="))
        command = argv[argv.index("--") + 1:]
        proc = subprocess.Popen(command, cwd=props["WorkingDirectory"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        timer = threading.Timer(float(props["RuntimeMaxSec"]), self.kill_all)
        timer.daemon = True
        timer.start()
        threading.Thread(target=lambda: (proc.wait(), timer.cancel()), daemon=True).start()
        return proc

    def stop(self, turn):
        self.stops.append(turn)
        self.kill_all()

    def kill_all(self):
        subprocess.run(["pkill", "-KILL", "-f", self.marker], capture_output=True)

    def close(self):
        self.kill_all()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)
