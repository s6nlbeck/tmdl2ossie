"""Render an Ossie document as a Graphviz entity-relationship diagram.

Produces DOT source only; Streamlit's ``graphviz_chart`` lays it out in the
browser, so no Graphviz installation is needed.

The input is the document's JSON form as the editor holds it, which may have
been hand-edited into something inconsistent. Nothing here assumes the document
is valid: a relationship pointing at a dataset that does not exist is simply not
drawn -- the validator is what reports it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from html import escape
from typing import Any

from pbi_semantik.mapping_helpers import POWER_BI_VENDOR

# Reference categorical palette (light mode), slots 1-3, plus neutral inks.
# The role is also spelled out in each header, so color is never the only cue.
ROLE_COLORS = {
    "Dimension": "#2a78d6",
    "Fakt": "#eb6834",
    "Tabelle": "#52514e",
}
METRIC_COLOR = "#1baf7a"
SURFACE = "#fcfcfb"
BORDER = "#d3d2cc"
INK = "#0b0b0b"
INK_MUTED = "#6b6a66"
EDGE = "#8a8984"
FONT = "Helvetica"


def to_dot(
    document: dict[str, Any],
    *,
    show_fields: bool = True,
    show_metrics: bool = True,
    direction: str = "LR",
) -> str:
    """Build DOT source for *document*.

    Datasets that are only ever the target of relationships are drawn as
    dimensions, those that only point outwards as facts. Edges run from the
    foreign-key column to the referenced key, with a crow's foot on the many side.
    """
    datasets = [item for item in _list(document.get("datasets")) if isinstance(item, dict) and item.get("name")]
    relationships = [item for item in _list(document.get("relationships")) if isinstance(item, dict)]
    names = {dataset["name"] for dataset in datasets}

    foreign_keys: dict[str, set[str]] = {}
    sources: set[str] = set()
    targets: set[str] = set()
    for relationship in relationships:
        source, target = relationship.get("from"), relationship.get("to")
        if source in names and target in names:
            sources.add(source)
            targets.add(target)
            foreign_keys.setdefault(source, set()).update(_strings(relationship.get("from_columns")))

    metrics_by_table, loose_metrics = _group_metrics(document, names) if show_metrics else ({}, [])

    lines = [
        "digraph ossie {",
        f'  graph [rankdir={_direction(direction)}, bgcolor="transparent", pad=0.4, '
        f'nodesep=0.5, ranksep=1.1, fontname="{FONT}"];',
        f'  node [shape=plain, fontname="{FONT}", fontsize=11];',
        f'  edge [color="{EDGE}", penwidth=1.4, arrowsize=0.9, dir=both, '
        f'arrowtail=crow, arrowhead=tee, fontname="{FONT}"];',
    ]

    ports: dict[str, dict[str, str]] = {}
    for dataset in datasets:
        name = dataset["name"]
        role = _role(name, sources, targets)
        label, ports[name] = _dataset_label(
            dataset,
            role=role,
            foreign_keys=foreign_keys.get(name, set()),
            metrics=metrics_by_table.get(name, []),
            show_fields=show_fields,
        )
        tooltip = dataset.get("description") or f"{role}: {name}"
        lines.append(f"  {_id(name)} [label=<{label}>, tooltip={_quote(tooltip)}];")

    if loose_metrics:
        lines.append(f"  {_id('__metrics__')} [label=<{_metrics_label(loose_metrics)}>];")

    for relationship in relationships:
        source, target = relationship.get("from"), relationship.get("to")
        if source not in names or target not in names:
            continue
        from_columns = _strings(relationship.get("from_columns"))
        to_columns = _strings(relationship.get("to_columns"))
        # Left to right, facts sit left of their dimensions: leave a row on the
        # right, enter the key row from the left. Other directions let
        # Graphviz pick the side.
        horizontal = _direction(direction) in {"LR", "RL"}
        tail = _endpoint(source, from_columns, ports, "r:e" if horizontal else "r")
        head = _endpoint(target, to_columns, ports, "l:w" if horizontal else "l")
        tooltip = (
            f"{relationship.get('name', '')}: "
            f"{source}.{', '.join(from_columns)} → {target}.{', '.join(to_columns)}"
        )
        lines.append(f"  {tail} -> {head} [tooltip={_quote(tooltip)}];")

    lines.append("}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Node labels (Graphviz HTML-like labels)
# --------------------------------------------------------------------------- #


def _dataset_label(
    dataset: dict[str, Any],
    *,
    role: str,
    foreign_keys: set[str],
    metrics: list[dict[str, Any]],
    show_fields: bool,
) -> tuple[str, dict[str, str]]:
    fields = [item for item in _list(dataset.get("fields")) if isinstance(item, dict) and item.get("name")]
    primary = set(_strings(dataset.get("primary_key")))
    unique = {column for key in _list(dataset.get("unique_keys")) for column in _strings(key)}

    rows = [_header(dataset["name"], role, ROLE_COLORS[role])]
    ports: dict[str, str] = {}
    if show_fields:
        for index, field in enumerate(fields):
            name = field["name"]
            port = f"f{index}"
            ports[name] = port
            badge = "PK" if name in primary else "UK" if name in unique else "FK" if name in foreign_keys else ""
            # A port on each outer cell, so edges meet the row at the table's
            # edge instead of cutting through the datatype column.
            rows.append(
                "<TR>"
                f'<TD ALIGN="LEFT" WIDTH="22" PORT="{port}l">{_small(badge, INK_MUTED, bold=True)}</TD>'
                f'<TD ALIGN="LEFT"><FONT COLOR="{INK}">{escape(name)}</FONT></TD>'
                f'<TD ALIGN="RIGHT" PORT="{port}r">{_small(field.get("datatype") or "", INK_MUTED)}</TD>'
                "</TR>"
            )
    else:
        rows.append(f'<TR><TD COLSPAN="3" ALIGN="LEFT">{_small(f"{len(fields)} Felder", INK_MUTED)}</TD></TR>')

    if metrics:
        rows.append(f'<TR><TD COLSPAN="3" HEIGHT="1" BGCOLOR="{BORDER}"></TD></TR>')
        rows += [_metric_row(metric) for metric in metrics]

    return _table(rows), ports


def _metrics_label(metrics: list[dict[str, Any]]) -> str:
    return _table([_header("Metriken", "ohne Tabelle", METRIC_COLOR), *(_metric_row(m) for m in metrics)])


def _metric_row(metric: dict[str, Any]) -> str:
    return (
        "<TR>"
        f'<TD ALIGN="LEFT" WIDTH="22">{_small("ƒ", METRIC_COLOR, bold=True)}</TD>'
        f'<TD ALIGN="LEFT"><FONT COLOR="{INK}">{escape(str(metric["name"]))}</FONT></TD>'
        f'<TD ALIGN="RIGHT">{_small(metric.get("datatype") or "Metrik", INK_MUTED)}</TD>'
        "</TR>"
    )


def _header(title: str, caption: str, color: str) -> str:
    return (
        f'<TR><TD COLSPAN="3" ALIGN="LEFT" BGCOLOR="{color}" CELLPADDING="8">'
        f'<FONT COLOR="#ffffff" POINT-SIZE="13"><B>{escape(title)}</B></FONT>'
        f'<FONT COLOR="#ffffff" POINT-SIZE="9">   {escape(caption)}</FONT>'
        "</TD></TR>"
    )


def _table(rows: Iterable[str]) -> str:
    return (
        f'<TABLE BORDER="1" COLOR="{BORDER}" CELLBORDER="0" CELLSPACING="0" '
        f'CELLPADDING="5" BGCOLOR="{SURFACE}">' + "".join(rows) + "</TABLE>"
    )


def _small(text: str, color: str, *, bold: bool = False) -> str:
    if not text:
        return " "
    body = f"<B>{escape(text)}</B>" if bold else escape(text)
    return f'<FONT COLOR="{color}" POINT-SIZE="9">{body}</FONT>'


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _role(name: str, sources: set[str], targets: set[str]) -> str:
    if name in targets and name not in sources:
        return "Dimension"
    if name in sources and name not in targets:
        return "Fakt"
    return "Tabelle"


def _group_metrics(document: dict[str, Any], names: set[str]) -> tuple[dict[str, list], list]:
    """Attach each metric to its Power BI home table, where the extension records one."""
    by_table: dict[str, list[dict[str, Any]]] = {}
    loose: list[dict[str, Any]] = []
    for metric in _list(document.get("metrics")):
        if not isinstance(metric, dict) or not metric.get("name"):
            continue
        table = power_bi_extension(metric).get("table")
        if table in names:
            by_table.setdefault(table, []).append(metric)
        else:
            loose.append(metric)
    return by_table, loose


def power_bi_extension(item: dict[str, Any]) -> dict[str, Any]:
    """Decode the Power BI ``custom_extensions`` payload of an Ossie object, if any."""
    for extension in _list(item.get("custom_extensions")):
        if isinstance(extension, dict) and extension.get("vendor_name") == POWER_BI_VENDOR:
            try:
                data = json.loads(extension.get("data") or "{}")
            except (TypeError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _endpoint(dataset: str, columns: list[str], ports: dict[str, dict[str, str]], side: str) -> str:
    port = ports.get(dataset, {}).get(columns[0]) if columns else None
    return f"{_id(dataset)}:{port}{side}" if port else _id(dataset)


def _direction(direction: str) -> str:
    return direction if direction in {"LR", "TB", "RL", "BT"} else "LR"


def _id(name: str) -> str:
    return _quote(f"ds_{name}")


def _quote(text: str) -> str:
    # DOT strings only need backslashes and double quotes escaped; newlines
    # would end up as literal line breaks in tooltips, which is fine.
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [str(item) for item in _list(value)]
