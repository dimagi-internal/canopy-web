"""The server's keep-alive must outlast the shared ALB's idle timeout (see the Dockerfile CMD).

If uvicorn closes an idle connection first, the ALB can send a request down it as it closes and
answer the caller with a 502 the app never saw."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: idle_timeout.timeout_seconds on the shared labs ALB (not managed in this repo).
ALB_IDLE_TIMEOUT_S = 600


def _cmd() -> list[str]:
    body = (ROOT / "Dockerfile").read_text().split("\nCMD [", 1)[1].split("]", 1)[0]
    return [part.strip().strip('"') for part in body.replace("\\\n", " ").split(",")]


def test_keep_alive_outlives_the_alb_idle_timeout():
    cmd = _cmd()
    assert "--timeout-keep-alive" in cmd, cmd
    assert int(cmd[cmd.index("--timeout-keep-alive") + 1]) > ALB_IDLE_TIMEOUT_S
