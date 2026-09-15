"""OpenObserve dashboard body builder (v0.91 schema).

The OpenObserve dashboard create API expects a large, deeply-nested JSON body
(tabs -> panels -> config[dozens of keys] + queries[fields x/y/z/breakdown/filter]
+ layout). Making the LLM emit all of that verbatim is brittle and was the cause
of "failed ... error (panel ...)" on create.

Instead the LLM provides a SMALL high-level spec (title + a list of panels, each
with a chart type, a SQL query, and the axis field/aggregation), and this module
assembles a complete, schema-valid body. All the boilerplate lives here in code.

High-level panel spec (what the tool accepts):
    {
      "title": "Log Event Dashboard",
      "description": "...",              # optional
      "panels": [
        {
          "title": "Events over time",
          "type": "line",               # line|bar|area|pie|table|scatter ...
          "query": "SELECT histogram(_timestamp) as \"x_axis_1\", ...",
          "stream": "aruba",
          "x": {"field": "_timestamp", "alias": "x_axis_1", "function": "histogram"},
          "y": {"field": "_timestamp", "alias": "y_axis_1", "function": "count"},
          "breakdown": {"field": "ap_name", "alias": "z_axis_1"}   # optional (z axis)
        },
        ...
      ]
    }
"""
from __future__ import annotations

import time
from typing import Any


def _panel_config() -> dict:
    """The large per-panel config block with OpenObserve's expected defaults."""
    return {
        "show_legends": True,
        "legends_position": None,
        "decimals": 2,
        "line_thickness": 1.5,
        "step_value": "0",
        "top_results_others": False,
        "axis_border_show": False,
        "label_option": {"rotate": 0},
        "axis_label_rotate": 0,
        "show_symbol": False,
        "line_interpolation": "smooth",
        "legend_width": {"unit": "px"},
        "legend_height": {"unit": "px"},
        "base_map": {"type": "osm"},
        "map_type": {"type": "world"},
        "map_view": {"zoom": 1, "lat": 0, "lng": 0},
        "map_symbol_style": {
            "size": "by Value",
            "size_by_value": {"min": 1, "max": 100},
            "size_fixed": 2,
        },
        "drilldown": [],
        "mark_line": [],
        "override_config": [],
        "connect_nulls": False,
        "no_value_replacement": "",
        "wrap_table_cells": False,
        "table_transpose": False,
        "table_dynamic_columns": False,
        "mappings": [],
        "color": {
            "mode": "palette-classic-by-series",
            "fixedColor": ["#53ca53"],
            "seriesBy": "last",
            "colorBySeries": [],
        },
        "trellis": {"layout": None, "num_of_columns": 1, "group_by_y_axis": False},
        "show_gridlines": True,
        "aggregation": "last",
        "lat_label": "latitude",
        "lon_label": "longitude",
        "weight_label": "weight",
        "name_label": "name",
        "table_aggregations": ["last"],
        "promql_table_mode": "single",
        "visible_columns": [],
        "hidden_columns": [],
        "sticky_columns": [],
        "sticky_first_column": False,
        "column_order": [],
        "table_pagination": False,
        "table_pivot_show_row_totals": False,
        "table_pivot_show_col_totals": False,
        "table_pivot_sticky_row_totals": False,
        "table_pivot_sticky_col_totals": False,
        "panel_time_enabled": False,
    }


def _query_config() -> dict:
    return {
        "promql_legend": "",
        "layer_type": "scatter",
        "weight_fixed": 1,
        "limit": 0,
        "min": 0,
        "max": 100,
        "time_shift": [],
        "query_label": "",
    }


def _axis_item(spec: dict, default_alias: str, *, with_interval: bool = False) -> dict:
    """Build an x/y axis field descriptor from a small spec."""
    field = spec.get("field") or "_timestamp"
    alias = spec.get("alias") or default_alias
    fn = spec.get("function")  # histogram|count|sum|avg|min|max|None
    args: list[dict] = [{"type": "field",
                         "value": {"field": field, "streamAlias": None}}]
    if with_interval and fn == "histogram":
        args.append({"type": "histogramInterval"})
    item = {
        "label": field,
        "alias": alias,
        "column": field,
        "type": "build",
        "color": None,
        "args": args,
        "isDerived": False,
        "havingConditions": [],
        "treatAsNonTimestamp": False,
        "showFieldAsJson": False,
    }
    if fn:
        item["functionName"] = fn
    if spec.get("sortBy"):
        item["sortBy"] = spec["sortBy"]
    return item


def _breakdown_item(spec: dict, default_alias: str) -> dict:
    field = spec.get("field")
    alias = spec.get("alias") or default_alias
    return {
        "label": field,
        "alias": alias,
        "column": field,
        "type": "build",
        "color": None,
        "args": [{"type": "field", "value": {"field": field, "streamAlias": None}}],
        "isDerived": False,
        "havingConditions": [],
    }


def _build_panel(idx: int, p: dict, default_stream: str) -> dict:
    stream = p.get("stream") or default_stream
    x_spec = p.get("x") or {"field": "_timestamp", "alias": "x_axis_1",
                            "function": "histogram", "sortBy": "ASC"}
    y_spec = p.get("y") or {"field": "_timestamp", "alias": "y_axis_1",
                            "function": "count"}
    bd_spec = p.get("breakdown")

    fields: dict[str, Any] = {
        "stream": stream,
        "stream_type": p.get("stream_type", "logs"),
        "x": [_axis_item(x_spec, "x_axis_1", with_interval=True)],
        "y": [_axis_item(y_spec, "y_axis_1")],
        "z": [],
        "breakdown": [_breakdown_item(bd_spec, "z_axis_1")] if bd_spec else [],
        "filter": {"filterType": "group", "logicalOperator": "AND", "conditions": []},
    }

    # two-per-row layout: 96-wide half columns, height 18
    col = idx % 2
    row = idx // 2
    layout = {"x": col * 96, "y": row * 18, "w": 96, "h": 18, "i": idx + 1}

    return {
        "id": f"Panel_ID{int(time.time()*1000) % 10_000_000}{idx}",
        "type": p.get("type", "line"),
        "title": p.get("title") or f"Panel {idx + 1}",
        "description": p.get("description", ""),
        "config": _panel_config(),
        "queryType": "sql",
        "queries": [
            {
                "query": p["query"],
                "vrlFunctionQuery": "",
                "customQuery": True,
                "fields": fields,
                "config": _query_config(),
                "joins": [],
            }
        ],
        "layout": layout,
        "htmlContent": "",
        "markdownContent": "",
        "customChartContent": "option = {};",
    }


def build_dashboard_body(spec: dict) -> dict:
    """Assemble a full OpenObserve v0.91 dashboard body from a small spec.

    Required: spec["title"], spec["panels"] (each with at least "query").
    Raises ValueError on missing essentials so the gate reports a clear error.
    """
    title = (spec.get("title") or "").strip()
    if not title:
        raise ValueError("dashboard title is required")
    panels_spec = spec.get("panels") or []
    if not panels_spec:
        raise ValueError("at least one panel is required")
    for i, p in enumerate(panels_spec):
        if not (p.get("query") or "").strip():
            raise ValueError(f"panel {i + 1} is missing a SQL 'query'")

    default_stream = spec.get("stream") or ""
    panels = [_build_panel(i, p, default_stream) for i, p in enumerate(panels_spec)]

    return {
        "version": 8,
        "title": title,
        "description": spec.get("description", ""),
        "role": "",
        "owner": "",
        "tabs": [{"tabId": "default", "name": "Default", "panels": panels}],
        "variables": {"list": [], "showDynamicFilters": True},
        "defaultDatetimeDuration": {
            "type": "relative",
            "relativeTimePeriod": spec.get("time_period", "15m"),
        },
    }
