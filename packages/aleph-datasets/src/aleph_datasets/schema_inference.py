"""Infer column types from row dicts.

Type hierarchy: int < float < bool < string < null.  Each column's
inferred type is the most-specific that all observed values fit.
Geometry columns (GeoJSON-shaped dicts) and explicitly typed columns
are passed through.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _infer_value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, dict) and v.get("type") in {
        "Point",
        "LineString",
        "Polygon",
        "MultiPoint",
        "MultiLineString",
        "MultiPolygon",
        "GeometryCollection",
        "Feature",
        "FeatureCollection",
    }:
        return "geometry"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "string"


_PROMOTION = {
    ("null", "int"): "int",
    ("null", "float"): "float",
    ("null", "bool"): "bool",
    ("null", "string"): "string",
    ("int", "float"): "float",
    ("bool", "int"): "int",
    ("bool", "float"): "float",
}


def _promote(a: str, b: str) -> str:
    if a == b:
        return a
    key = tuple(sorted((a, b)))
    if key in _PROMOTION:
        return _PROMOTION[key]
    return "string"  # widest


def infer_column_schema(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return [{name, type}] ordered by first appearance in rows."""
    order: list[str] = []
    types: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            if k not in types:
                order.append(k)
                types[k] = _infer_value(v)
            else:
                types[k] = _promote(types[k], _infer_value(v))
    return [{"name": k, "type": types[k]} for k in order]
