"""Reading a CloudFormation template under deploy/aws, with its `!Ref`-style tags as plain mappings."""
from __future__ import annotations

from pathlib import Path

import yaml

AWS = Path(__file__).resolve().parent.parent.parent / "deploy" / "aws"


class _L(yaml.SafeLoader):
    pass


_L.add_multi_constructor("!", lambda loader, suffix, node: {
    suffix: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
    else loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode)
    else loader.construct_mapping(node, deep=True)})


def load_template(name: str) -> dict:
    return yaml.load((AWS / name).read_text(), Loader=_L)
