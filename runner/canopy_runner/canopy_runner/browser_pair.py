"""`canopy-runner pair --browser` — pair this computer from a browser, with no PAT at all.

`canopy-runner pair` needs a canopy-web PAT on disk, which a person who downloaded the
installer does not have and should never need. This is the keyless path instead: the
runner makes its device key first, asks canopy-web for a pairing with a DPoP proof of
that key, and a person approves it in their browser behind a passkey step-up. canopy-web
then hands the approval's one-time secret back ONLY through a redirect to
`http://127.0.0.1:<port>/paired?…` — a tiny server this command runs on this machine's
loopback. The runner claims with that secret and a fresh proof of the same key, and gets
a runner whose device key is already ACTIVE.

The loopback leg is the security, not a convenience: someone who phishes a person into
approving a code never receives the secret, because the redirect lands on the VICTIM's
own loopback, where nothing of the attacker's is listening. So the server answers only
for the pairing id it is waiting for, never logs a request line (the secret is in the
query), and is gone as soon as the pairing ends.

While the person is in the browser this command polls the claim WITHOUT the secret
(`202 waiting`) — that is how it learns the pairing was declined or expired (a uniform
401) and can say so instead of waiting out the clock. `~/.canopy/pairing.json` carries
the code and link for the menu-bar app to show.

What it writes is what canopy's own pairing would leave: `~/.canopy/runner.json` with an
EMPTY token (there is no PAT; `runner_token` signs in with the key), and
`~/.canopy/device-key/status.json` marking the key active for this runner, so a later
`canopy vault …` on the same box uses the same key.
"""
from __future__ import annotations

import datetime as dt
import html
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from . import device_proof, pair
from .platform_jobs import process_account
from .runner_token import Transport, urllib_transport

PAIRINGS_PATH = "/api/harness/runner-pairings/"
#: How long to wait when canopy-web gives no `expires_at` (its pairings live 15 min).
DEFAULT_TTL = 15 * 60
DEFAULT_INTERVAL = 5

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Canopy Runner paired</title>
<style>body{{font:16px/1.5 -apple-system,system-ui,sans-serif;margin:0;display:grid;
place-items:center;min-height:100vh;background:#f7f7f5;color:#1c1c1a}}
main{{max-width:28rem;padding:2rem;text-align:center}}
h1{{font-size:1.4rem;margin:0 0 .5rem}}p{{margin:0;color:#5b5b57}}</style></head>
<body><main><h1>{title}</h1><p>{text}</p></main></body></html>"""


def pairing_file(home: Path) -> Path:
    return home / ".canopy" / "pairing.json"


def platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "linux"


def short_hostname() -> str:
    try:
        return socket.gethostname().split(".")[0] or "this-computer"
    except OSError:
        return "this-computer"


def has_emdash(home: Path) -> bool:
    if pair.find_emdash_db(home).exists():
        return True
    if sys.platform == "darwin":
        return any(p.exists() for p in (Path("/Applications/Emdash.app"),
                                        home / "Applications" / "Emdash.app"))
    return False


def _expires(value, now: float) -> float:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return now + DEFAULT_TTL


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --- the loopback ---------------------------------------------------------------


class Loopback:
    """A one-pairing HTTP server on 127.0.0.1. It accepts `GET /paired` for the pairing
    id it is told to expect, keeps that secret, and answers everything else 404."""

    def __init__(self) -> None:
        self.expected_id = ""
        self.secret = ""
        self.got = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # the query carries the secret
                return

            def do_GET(self) -> None:  # noqa: N802 — http.server's name
                parts = urlsplit(self.path)
                qs = parse_qs(parts.query)
                pid = (qs.get("pairing") or [""])[0]
                secret = (qs.get("secret") or [""])[0]
                if (parts.path.rstrip("/") == "/paired" and outer.expected_id
                        and pid == outer.expected_id and secret and not outer.got.is_set()):
                    outer.secret = secret
                    outer.got.set()
                    self._page(200, "Paired — you can close this tab",
                               "This computer is now connected to Canopy. The runner "
                               "is finishing its setup.")
                    return
                self._page(404, "Nothing to do here",
                           "This address only answers the Canopy Runner pairing it is "
                           "waiting for.")

            def _page(self, code: int, title: str, text: str) -> None:
                body = _PAGE.format(title=html.escape(title), text=html.escape(text)).encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                        name="pair-loopback")

    def start(self) -> "Loopback":
        self._thread.start()
        return self

    def close(self) -> None:
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()


# --- canopy-web -------------------------------------------------------------------


class Api:
    """The two anonymous, DPoP-proved pairing calls."""

    def __init__(self, base_url: str, key: device_proof.DeviceKey,
                 transport: Transport = urllib_transport):
        self.base = base_url.rstrip("/")
        self.key = key
        self.transport = transport

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        from .client import user_agent

        url = self.base + path
        headers = {"Content-Type": "application/json", "User-Agent": user_agent(),
                   "DPoP": device_proof.dpop_proof(self.key, "POST", url)}
        status, text = self.transport("POST", url, headers, json.dumps(body).encode("utf-8"))
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        return status, data if isinstance(data, dict) else {}


def _open_browser(url: str, *, run=subprocess.run, opener=webbrowser.open) -> bool:
    try:
        if sys.platform == "darwin":
            return run(["open", url], capture_output=True, timeout=15).returncode == 0
        return bool(opener(url))
    except Exception:  # noqa: BLE001 — the printed link is the fallback
        return False


# --- orchestration -----------------------------------------------------------------


def run_browser_pair(config_path: Path, *, base_url: str = pair.DEFAULT_BASE_URL,
                     allow_software: Optional[bool] = None, home: Optional[Path] = None,
                     users_root: Optional[Path] = None, transport: Transport = urllib_transport,
                     open_browser: Callable[[str], bool] = _open_browser,
                     ensure_key=device_proof.ensure, ensure_ecdh=device_proof.ensure_ecdh,
                     is_free=pair.port_free, live_cdp_port=pair.emdash_live_cdp_port,
                     clock: Callable[[], float] = time.time, out=print,
                     loopback: Optional[Loopback] = None) -> int:
    """Pair this computer from the browser and write its runner.json. 0 on success
    (including "already paired"); raises pair.PairError to refuse."""
    home = home or Path.home()
    config_path = Path(config_path).expanduser()
    if config_path.exists():
        out(f"This computer is already paired ({config_path}). Nothing to do.")
        return 0

    try:
        key, created = ensure_key(allow_software=allow_software)
    except device_proof.DeviceKeyError as exc:
        raise pair.PairError(f"could not make this computer's device key: {exc}") from None
    out(f"==> {'made' if created else 'using'} this computer's {key.kind} device key "
        f"({key.jkt[:12]}…)")
    ecdh = None
    try:
        ecdh, _ = ensure_ecdh(key.kind)
    except device_proof.DeviceKeyError as exc:
        out(f"    (no ECDH key: {exc} — pairing without it)")

    lb = loopback or Loopback()
    lb.start()
    pf = pairing_file(home)
    try:
        api = Api(base_url, key, transport)
        body = {"public_jwk": key.public_jwk, "kind": key.kind,
                "hostname": short_hostname(), "account": process_account(),
                "platform": platform_name(), "has_emdash": has_emdash(home),
                "redirect_port": lb.port}
        if ecdh is not None:
            body["ecdh_public_jwk"] = ecdh.public_jwk
            body["ecdh_store"] = ecdh.kind
        status, ans = api.post(PAIRINGS_PATH, body)
        if status == 200 and ans.get("status") == "reattached":
            out(f"==> this computer is already a runner on Canopy: {ans.get('name')} "
                f"({ans.get('workspace')}) — reconnecting it")
            return _finish(config_path, ans, key=key, ecdh=ecdh, base_url=base_url, home=home,
                           users_root=users_root, is_free=is_free,
                           live_cdp_port=live_cdp_port, out=out)
        if status != 201 or not ans.get("id") or not ans.get("user_code"):
            raise pair.PairError(f"Canopy did not start a pairing (HTTP {status}) — check the "
                                 f"address ({base_url}) and your connection, then try again")
        pid, code = str(ans["id"]), str(ans["user_code"])
        verify_url = str(ans.get("verify_url") or f"{base_url.rstrip('/')}/pair/{code}")
        interval = max(1, int(ans.get("interval") or DEFAULT_INTERVAL))
        deadline = _expires(ans.get("expires_at"), clock())
        lb.expected_id = pid
        _write_json(pf, {"user_code": code, "verify_url": verify_url, "status": "pending",
                         "expires_at": ans.get("expires_at") or ""})
        out("")
        out(f"  Approve this computer in your browser — code {code}")
        out(f"  {verify_url}")
        out("")
        if not open_browser(verify_url):
            out("  (could not open a browser — open the link above yourself)")
        claim_path = f"{PAIRINGS_PATH}{pid}/claim"
        while True:
            if lb.got.wait(timeout=interval):
                break
            if clock() >= deadline:
                raise pair.PairError("the pairing expired before it was approved — run setup "
                                     "again for a new code")
            st, _ = api.post(claim_path, {})
            if st == 202:
                continue
            if st == 401:
                raise pair.PairError("the pairing was declined or has expired — run setup again "
                                     "for a new code")
            if st == 201:
                # Approved and claimed without the secret can't happen by contract; say so.
                raise pair.PairError("Canopy answered the pairing without its secret — refusing")
            # Anything else (a 5xx, a network blip) is retried on the next interval.
        st, ans = api.post(claim_path, {"secret": lb.secret})
        if st != 201 or ans.get("status") != "paired" or not ans.get("runner_id"):
            raise pair.PairError(f"Canopy refused the pairing (HTTP {st}) — it may have been "
                                 "approved in a browser on a different computer, or expired. "
                                 "Run setup again for a new code.")
        out(f"==> approved: this computer is runner {ans.get('name')} in {ans.get('workspace')}")
        rc = _finish(config_path, ans, key=key, ecdh=ecdh, base_url=base_url, home=home,
                     users_root=users_root, is_free=is_free, live_cdp_port=live_cdp_port, out=out)
        try:
            pf.unlink()
        except OSError:
            pass
        return rc
    except pair.PairError as exc:
        try:
            if pf.exists():
                _write_json(pf, {**json.loads(pf.read_text(encoding="utf-8")),
                                 "status": "failed", "error": str(exc)[:300]})
        except (OSError, ValueError):
            pass
        raise
    finally:
        lb.close()


def _finish(config_path: Path, ans: dict, *, key, ecdh, base_url: str, home: Path,
            users_root: Optional[Path], is_free, live_cdp_port, out) -> int:
    runner_id = str(ans.get("runner_id") or "")
    if not runner_id:
        raise pair.PairError(f"Canopy named no runner: {ans!r}")
    users_root = users_root or home.parent
    taken = pair.sibling_ports(users_root, home)
    live = live_cdp_port(home)
    cdp = live if live is not None and live not in taken else pair.choose_port(
        pair.CDP_BASE, taken, is_free)
    hook = pair.choose_port(pair.HOOK_BASE, taken | {cdp}, is_free)
    db = pair.find_emdash_db(home)
    data = pair.build_config(base_url=base_url, token_ref="", runner_id=runner_id,
                             emdash_db=db, cdp_port=cdp, hook_port=hook)
    if not db.exists():
        data["emdash_db"] = ""
    # The key first: a runner.json without its active status would start a daemon
    # that cannot sign in until something rewrote it.
    device_proof.write_status(device_proof.registered_status(
        key, key_id=ans.get("device_key_id"), runner_id=runner_id, status="active", ecdh=ecdh))
    pair.write_config(config_path, data)
    out(f"==> wrote {config_path} (no token: this runner signs in with its device key)")
    return 0
