"""The zero-knowledge vault's reference implementation and test vectors (docs/vault/zk-v1),
loaded once for every suite that checks against them.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ZK_DIR = Path(__file__).resolve().parents[2] / "docs" / "vault" / "zk-v1"


def _load_reference():
    spec = importlib.util.spec_from_file_location("zk_v1_reference", ZK_DIR / "zk_v1_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ref = _load_reference()


def _load_connections_reference():
    spec = importlib.util.spec_from_file_location("connections_v1_reference", ZK_DIR / "connections_v1_reference.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: The connections wire format's reference (`docs/vault/zk-v1/connections_v1_reference.py`):
#: what the owner's browser and the CLI do, signing statements and writing envelope MACs.
conn_ref = _load_connections_reference()


VECTORS = json.loads((ZK_DIR / "zk_v1_vectors.json").read_text())
