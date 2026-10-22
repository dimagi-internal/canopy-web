"""No two schema classes published in the OpenAPI share a component name.

Ninja emits ONE `components.schemas` entry per name (the class's `__name__`) and
a later definition silently overwrites an earlier one. So two classes both
called `ActOut` in different apps leave every `$ref` to that name pointing at
whichever won. A field typed as one schema then appears in the docs, and in
`frontend/src/api/generated.ts`, as the other. Give one of them a distinct name
(e.g. `StoryboardActOut`).
"""
from __future__ import annotations

import typing
from collections import defaultdict

from pydantic import BaseModel

from apps.api.api import api


def _models_in(annotation) -> typing.Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for arg in typing.get_args(annotation):
        yield from _models_in(arg)


def _published_models() -> set[type[BaseModel]]:
    """Every model reachable from a route the schema includes, through its fields."""
    stack: list[type[BaseModel]] = []
    for bound in api._get_bound_routers():
        for path_view in bound.path_operations.values():
            for op in path_view.operations:
                if not op.include_in_schema:
                    continue
                for model in [*(op.models or []), *(op.response_models or {}).values()]:
                    stack.extend(_models_in(model))
    seen: set[type[BaseModel]] = set()
    while stack:
        model = stack.pop()
        if model in seen:
            continue
        seen.add(model)
        for field in model.model_fields.values():
            stack.extend(_models_in(field.annotation))
    return seen


def test_the_walk_reaches_the_schemas_it_should():
    names = {m.__name__ for m in _published_models()}
    assert {"StoryboardOut", "AgentTaskOut"} <= names


def test_no_two_published_schemas_share_a_component_name():
    by_name: dict[str, set[str]] = defaultdict(set)
    for model in _published_models():
        if not model.__module__.startswith("apps."):
            continue  # ninja's own per-operation wrappers, flattened away
        by_name[model.__name__].add(f"{model.__module__}.{model.__qualname__}")
    clashes = {name: sorted(where) for name, where in by_name.items() if len(where) > 1}
    assert not clashes, (
        "schema classes sharing an OpenAPI component name (one silently "
        f"overwrites the other in components.schemas): {clashes}"
    )
