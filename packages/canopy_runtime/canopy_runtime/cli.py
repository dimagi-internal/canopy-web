"""``python -m canopy_runtime.cli`` — reconcile a box for an agent, then (optionally)
export the resolved env or exec the turn.

This is the reconciler's operational face, used identically on a laptop and the
cloud runner (RS3). It builds the vault list from the agent slug
(``[Agent-<Slug>, <shared>]``), reconciles, reports gaps, and:

    --print-env   print `export K=V` lines for the resolved secrets+literals
    --env-file P  write those as a 0600 env file (e.g. ~/.echo/.env)
    --exec CMD…   run CMD with the resolved env merged in (only if ready)

Exit codes: 0 ready · 3 gaps remain (needs bootstrap) · 2 usage/spec error.

Also the ``canopy-runtime`` console script, whose one subcommand is
``publish-capabilities`` (``canopy_runtime.publish``); with no subcommand it is
the reconciler above.
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from canopy_runtime.reconcile import LocalEnvironment, reconcile
from canopy_runtime.schema import load_runtime_yaml
from canopy_runtime.stores import EnvVarStore, OnePasswordStore


def agent_vault(slug: str) -> str:
    """echo -> Agent-Echo (capitalize only the first letter, like the bootstrap)."""
    return f"Agent-{slug[:1].upper()}{slug[1:]}"


def build_store(*, agent: str, shared: str, account: str | None, kind: str):
    if kind == "env":
        return EnvVarStore()
    return OnePasswordStore([agent_vault(agent), shared], account=account)


def _report(result, *, stream=sys.stderr) -> None:
    for g in result.gaps:
        tag = "NEEDS-BOOTSTRAP" if g.needs_human else "GAP"
        print(f"{tag} [{g.kind}] {g.name}: {g.detail}", file=stream)
    if result.applied:
        print(f"applied: {', '.join(result.applied)}", file=stream)
    print(f"{'READY' if result.ready else 'NOT READY'} "
          f"({len(result.env)} env vars, {len(result.gaps)} gaps)", file=stream)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "publish-capabilities":
        from canopy_runtime.publish import main as publish_main

        return publish_main(argv[1:])
    ap = argparse.ArgumentParser(prog="canopy-reconcile")
    ap.add_argument("--spec", required=True, help="path to the agent's runtime.yaml")
    ap.add_argument("--agent", required=True, help="agent slug (selects Agent-<Slug> vault)")
    ap.add_argument("--shared-vault", default="Canopy-Shared")
    ap.add_argument("--account", default=os.environ.get("OP_ACCOUNT"))
    ap.add_argument("--store", choices=["op", "env"], default="op",
                    help="secret store: op (1Password) or env (CANOPY_SECRET_*)")
    ap.add_argument("--dry-run", action="store_true", help="scan + report; apply nothing")
    ap.add_argument("--print-env", action="store_true", help="print export lines to stdout")
    ap.add_argument("--env-file", help="write resolved env to this file (0600)")
    ap.add_argument("--exec", nargs=argparse.REMAINDER, default=None,
                    help="run the rest of the line with the resolved env (only if ready)")
    args = ap.parse_args(argv)

    try:
        spec = load_runtime_yaml(Path(args.spec).read_text())
    except (OSError, ValueError) as exc:
        print(f"error: bad spec {args.spec}: {exc}", file=sys.stderr)
        return 2

    store = build_store(agent=args.agent, shared=args.shared_vault,
                        account=args.account, kind=args.store)
    result = reconcile(spec, store, LocalEnvironment(), apply=not args.dry_run)
    _report(result)

    if args.print_env:
        for k, v in result.env.items():
            print(f"export {k}={shlex.quote(v)}")
    if args.env_file:
        body = "".join(f"{k}={v}\n" for k, v in result.env.items())
        p = Path(os.path.expanduser(args.env_file))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        p.chmod(0o600)

    if args.exec:
        if not result.ready:
            print("refusing to exec: box is not ready (see gaps above)", file=sys.stderr)
            return 3
        merged = {**os.environ, **result.env}
        os.execvpe(args.exec[0], args.exec, merged)  # replaces this process

    return 0 if result.ready else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
