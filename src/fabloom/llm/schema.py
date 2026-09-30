"""Pydantic model -> JSON schema acceptable to strict structured output.

Strict mode wants every object closed (``additionalProperties: false``) and every property
listed in ``required``; it also rejects some keywords Pydantic emits (``default``, ``title``,
string ``format``s such as ``ipv4network``). Defaults still apply on our side: the model is
simply asked to be explicit about every field.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

_DROP = ("default", "title", "format")


def _walk(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item)
        return
    if not isinstance(node, dict):
        return
    for key in _DROP:
        node.pop(key, None)
    props = node.get("properties")
    if isinstance(props, dict):
        node["additionalProperties"] = False
        node["required"] = list(props)
        for sub in props.values():  # property names are data; never strip them
            _walk(sub)
    for key, value in node.items():
        if key != "properties":
            _walk(value)


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = copy.deepcopy(model.model_json_schema())
    _walk(schema)
    return schema
