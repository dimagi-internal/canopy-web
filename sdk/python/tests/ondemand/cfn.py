"""A tiny CloudFormation reader for the template tests: parse short-form
intrinsics, then RENDER the template for given parameters the way CloudFormation
would before creating anything — conditions evaluated, `Fn::If` resolved,
`AWS::NoValue` dropped, conditional resources and outputs removed.

Only what the capability template uses (Equals, Not, If, NoValue): anything else
is left as it is, so a test comparing two renders still sees it.
"""
from __future__ import annotations

import yaml

_DROP = object()


class _CfnLoader(yaml.SafeLoader):
    """Keeps CloudFormation short-form intrinsics as {"Fn": value} so the tree stays inspectable."""


def _intrinsic(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {tag_suffix: value}


_CfnLoader.add_multi_constructor("!", _intrinsic)


def load(text: str) -> dict:
    return yaml.load(text, Loader=_CfnLoader)


def _params(template: dict, overrides: dict) -> dict:
    values = {k: v.get("Default") for k, v in (template.get("Parameters") or {}).items()}
    values.update(overrides)
    return values


def _value(node, params):
    if isinstance(node, dict) and set(node) == {"Ref"}:
        return params.get(node["Ref"], node)
    return node


def _condition(expr, params, conds, raw):
    if isinstance(expr, dict) and len(expr) == 1:
        (fn, arg), = expr.items()
        if fn == "Equals":
            a, b = (_value(x, params) for x in arg)
            return str(a) == str(b)
        if fn == "Not":
            return not _condition(arg[0], params, conds, raw)
        if fn == "Condition":
            return _named(arg, params, conds, raw)
    raise ValueError(f"unsupported condition {expr!r}")


def _named(name, params, conds, raw):
    if name not in conds:
        conds[name] = _condition(raw[name], params, conds, raw)
    return conds[name]


def _resolve(node, truth):
    if isinstance(node, dict):
        if set(node) == {"If"}:
            name, yes, no = node["If"]
            return _resolve(yes if truth[name] else no, truth)
        if node == {"Ref": "AWS::NoValue"}:
            return _DROP
        out = {}
        for k, v in node.items():
            r = _resolve(v, truth)
            if r is not _DROP:
                out[k] = r
        return out
    if isinstance(node, list):
        return [r for r in (_resolve(v, truth) for v in node) if r is not _DROP]
    return node


def render(template: dict, **params) -> dict:
    """Resources and Outputs as CloudFormation would create them for `params`."""
    values = _params(template, params)
    raw = template.get("Conditions") or {}
    truth: dict[str, bool] = {}
    for name in raw:
        _named(name, values, truth, raw)
    out = {}
    for section in ("Resources", "Outputs"):
        kept = {}
        for name, body in (template.get(section) or {}).items():
            cond = body.get("Condition")
            if cond is not None and not truth[cond]:
                continue
            body = {k: v for k, v in body.items() if k != "Condition"}
            kept[name] = _resolve(body, truth)
        out[section] = kept
    return out
