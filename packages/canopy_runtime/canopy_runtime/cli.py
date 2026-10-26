"""The ``canopy-runtime`` console script. Its one subcommand is
``publish-capabilities`` (``canopy_runtime.publish``).

It used to also be a reconciler that resolved an agent's secrets from 1Password and
provisioned a box. No runner ever called it: an agent is prepared by canopy's
``canopy agent prepare`` and its env filled by ``canopy cred sync`` (canopy
docs/architecture/agent-setup-lifecycle.md), so it was retired (2026-10-15).
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "publish-capabilities":
        from canopy_runtime.publish import main as publish_main

        return publish_main(argv[1:])
    from canopy_runtime.publish import USAGE

    print(f"usage: canopy-runtime {USAGE.removeprefix('canopy-runtime ')}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
