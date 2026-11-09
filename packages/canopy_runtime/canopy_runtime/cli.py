"""The ``canopy-runtime`` console script. Its one subcommand is
``publish-capabilities`` (``canopy_runtime.publish``).

It is not a reconciler: it neither resolves an agent's secrets nor provisions a box.
An agent is prepared by canopy's ``canopy agent prepare`` and its env filled by
``canopy cred sync`` (the canopy repo's agent-setup-lifecycle doc).
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
