"""ChartCard spec compile — generate a Vega-Lite spec from a dataset version + axis hints.

This is a thin builder that produces a valid minimal Vega-Lite v6 spec.
The renderer (`@a2ui/react` or the homegrown Inc 4 renderer) feeds the
spec to react-vega.
"""

from __future__ import annotations

from typing import Any


def line_chart_spec(
    *,
    rows: list[dict[str, Any]],
    x_field: str,
    y_field: str,
    color_field: str | None = None,
    title: str = "",
) -> dict[str, Any]:
    encoding: dict[str, Any] = {
        "x": {"field": x_field, "type": "temporal" if x_field.lower().endswith("date") else "ordinal"},
        "y": {"field": y_field, "type": "quantitative"},
    }
    if color_field:
        encoding["color"] = {"field": color_field, "type": "nominal"}
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "title": title,
        "data": {"values": rows},
        "mark": "line",
        "encoding": encoding,
        "width": "container",
        "height": 240,
    }


def bar_chart_spec(
    *,
    rows: list[dict[str, Any]],
    x_field: str,
    y_field: str,
    title: str = "",
) -> dict[str, Any]:
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "title": title,
        "data": {"values": rows},
        "mark": "bar",
        "encoding": {
            "x": {"field": x_field, "type": "ordinal"},
            "y": {"field": y_field, "type": "quantitative"},
        },
        "width": "container",
        "height": 240,
    }


def scatter_chart_spec(
    *,
    rows: list[dict[str, Any]],
    x_field: str,
    y_field: str,
    color_field: str | None = None,
    title: str = "",
) -> dict[str, Any]:
    encoding: dict[str, Any] = {
        "x": {"field": x_field, "type": "quantitative"},
        "y": {"field": y_field, "type": "quantitative"},
    }
    if color_field:
        encoding["color"] = {"field": color_field, "type": "nominal"}
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
        "title": title,
        "data": {"values": rows},
        "mark": "circle",
        "encoding": encoding,
        "width": "container",
        "height": 240,
    }
