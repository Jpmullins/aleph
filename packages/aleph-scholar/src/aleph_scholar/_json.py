"""Typed navigation helpers for untyped upstream JSON payloads."""

from __future__ import annotations

from typing import Any, cast


def as_dict(value: object) -> dict[str, Any]:
    """`value` as a str-keyed dict, or empty dict when it isn't one."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[Any]:
    """`value` as a list, or empty list when it isn't one."""
    return cast("list[Any]", value) if isinstance(value, list) else []
