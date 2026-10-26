# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""EXPORT direction: Apache Ossie semantic model -> Databricks Metric View v1.1 YAML.

Python port of ``OssieToMetricView.java``. The hard part is reassembling the
relationship graph into a join tree: one fact table as ``source``, every other
dataset as a (possibly nested) join below it. Function names and order follow
the Java source so the two can be diffed side by side.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ossie_databricks._common import (
    CARD_MANY_TO_ONE,
    CARD_ONE_TO_MANY,
    MAX_JOIN_NODES,
    MV_VERSION,
    OSSIE_VERSION,
    STASH_SOURCE_KEY,
    SYNONYM_LIMIT,
    ConversionError,
    Notices,
    Result,
    as_list,
    as_map,
    dump_yaml,
    find_outside_literals,
    foreign_vendor_extensions,
    is_simple_identifier,
    jcat,
    jlist,
    jstr,
    load_yaml,
    merge_description,
    pick_expression,
    qualifier_chain_pattern,
    read_stash,
    replace_outside_literals,
    require,
    require_str,
    rewrite_qualifiers,
    str_list,
    synonyms_of,
    trim,
    truthy,
    validate_source,
)


@dataclass(eq=False)
class _Node:
    dataset: str
    rel: dict[str, Any]  # empty for the fact/root
    parent_is_from: bool
    alias: str = ""
    is_otm: bool = False
    children: list[_Node] = field(default_factory=list)


# -- public entry ----------------------------------------------------------------


def convert_ossie_to_metric_view(osi_yaml: str, source: str | None = None) -> Result:
    """Convert Ossie YAML text into Metric View YAML text.

    *source* names the fact dataset (the view's grain); by default it is the one
    dataset no relationship points at. Raises :class:`ConversionError` for input
    the converter cannot represent; anything dropped along the way is listed in
    ``Result.notices``.
    """
    notices = Notices()
    if osi_yaml is None or not trim(osi_yaml):
        raise ConversionError.invalid_input(
            "Invalid Apache Ossie YAML: expected a mapping at the root", "the input is empty"
        )
    try:
        parsed = load_yaml(osi_yaml)
    except Exception as error:
        raise ConversionError.invalid_input(
            f"Invalid Apache Ossie YAML: {error}", f"failed to parse YAML: {error}"
        ) from error
    if not isinstance(parsed, dict):
        raise ConversionError.invalid_input(
            "Invalid Apache Ossie YAML: expected a mapping at the root", "it is not a mapping at the root"
        )
    root = parsed
    version_value = root.get("version")
    version = jstr(version_value)
    if version_value is None or not trim(version):
        raise ConversionError.invalid_input(
            "Invalid Apache Ossie YAML: missing required 'version' field",
            "it is missing the required 'version' field",
        )
    if version != OSSIE_VERSION:
        raise ConversionError.unsupported(
            f"Unsupported Apache Ossie version '{version}'. Supported: {OSSIE_VERSION}", version
        )
    if "semantic_model" in root:
        reason = (
            "Legacy 'semantic_model' wrappers are not supported; place the model "
            "properties directly at the document root"
        )
        raise ConversionError.invalid_input(reason, reason)
    if "dialects" in root or "vendors" in root:
        reason = "Root 'dialects' and 'vendors' are not supported by the Apache Ossie spec"
        raise ConversionError.invalid_input(reason, reason)
    if not isinstance(root.get("name"), str):
        reason = "Apache Ossie model requires a string 'name' at the document root"
        raise ConversionError.invalid_input(reason, reason)
    view = _convert_model(root, source, notices)
    try:
        return Result(dump_yaml(view), notices.to_list())
    except Exception as error:
        raise ConversionError.internal(f"failed to serialize Metric View YAML: {error}") from error


def _convert_model(model: dict[str, Any], explicit_source: str | None, notices: Notices) -> dict[str, Any]:
    name = jstr(model.get("name")) if "name" in model else "<unnamed>"
    model_scope = f"Model '{name}'"
    dataset_list = _schema_map_list(model, "datasets", model_scope)
    if not dataset_list:
        raise ConversionError(f"Model '{name}' has no datasets")
    seen: set[str] = set()
    datasets: dict[str, dict[str, Any]] = {}
    dataset_fields: dict[str, list[dict[str, Any]]] = {}
    for dataset in dataset_list:
        ds_name = require_str(dataset, "name", f"Model '{name}': dataset")
        key = trim(ds_name).lower()
        if key in seen:
            raise ConversionError(f"Model '{name}': duplicate dataset name '{ds_name}'")
        seen.add(key)
        datasets[ds_name] = dataset
        dataset_fields[ds_name] = _schema_map_list(dataset, "fields", f"Dataset '{ds_name}'")
    relationships = _schema_map_list(model, "relationships", model_scope)
    metrics = _schema_map_list(model, "metrics", model_scope)

    model_stash = read_stash(model, notices, model_scope)
    fact_hint = explicit_source if explicit_source else jstr(model_stash.get(STASH_SOURCE_KEY))
    root, fact = _build_join_tree(name, datasets, relationships, fact_hint, notices)
    counts = _assign_aliases(root, fact)
    _mark_otm(name, root)

    fact_ds = datasets[fact]
    view: dict[str, Any] = {"version": MV_VERSION, "source": validate_source(fact_ds.get("source"), fact)}

    comment = jstr(model.get("description"))
    if truthy(comment):
        view["comment"] = comment
    if "filter" in model_stash:
        view["filter"] = model_stash["filter"]

    joins = [_build_join(child, "source", datasets, notices) for child in root.children]
    if joins:
        view["joins"] = joins

    dropped_dims: dict[str | None, None] = {}  # insertion-ordered sets
    dropped_measures: dict[str | None, None] = {}
    dimensions: list[dict[str, Any]] = []
    seen_dims: set[str] = set()
    # Dataset name -> the alias path addressing its columns from the primary source,
    # collected from the same walk as the dimensions so measures qualify identically.
    dataset_alias_path: dict[str, str] = {}
    for node, join_path in _node_order(root):
        is_fact = node is root
        prefix = node.alias if counts[node.dataset] > 1 else None
        qualifier = ".".join(join_path)
        if not is_fact:
            # A diamond dataset appears once per path; the first path wins.
            dataset_alias_path.setdefault(node.dataset, qualifier)
        for field_ in dataset_fields[node.dataset]:
            fname = require_str(field_, "name", f"dataset '{node.dataset}': field")
            if node.is_otm:
                notices.warn(
                    f"field '{fname}'",
                    "column on a one-to-many-joined table cannot be a dimension "
                    "(must resolve to one value per source row); dropped",
                )
                dropped_dims[fname] = None
                continue
            dim = _convert_field(field_, fname, qualifier, is_fact, prefix, notices)
            if dim is None:
                dropped_dims[fname] = None
                continue
            dim_name = dim["name"]
            if dim_name.lower() in seen_dims:
                raise ConversionError(
                    f"dataset '{node.dataset}': dimension name '{dim_name}' collides with another "
                    "dimension/measure; Metric Views require unique dimension/measure names -- rename before use"
                )
            seen_dims.add(dim_name.lower())
            dimensions.append(dim)

    measures: list[dict[str, Any]] = []
    fact_qualifier = re.compile(rf"\b{re.escape(fact)}\.", re.ASCII)
    dataset_head = qualifier_chain_pattern(dataset_alias_path)
    # Datasets reached by more than one join path: a measure naming one cannot be qualified.
    diamond_datasets = {joined for joined in dataset_alias_path if counts.get(joined, 1) > 1}
    for metric in metrics:
        measure = _convert_metric(
            metric, fact_qualifier, dataset_head, dataset_alias_path, diamond_datasets, seen_dims, notices
        )
        if measure is None:
            dropped_measures[jstr(metric.get("name"))] = None
            continue
        measures.append(measure)

    _CascadeDropper(dimensions, measures, dropped_dims, dropped_measures, notices).run()

    # A Metric View needs at least one dimension or measure; the engine refuses an
    # empty one at CREATE. After the cascade the emptiness is usually a consequence of
    # earlier drops, so name them -- that is the actionable part.
    if not dimensions and not measures:
        message = f"Model '{name}' produced no dimensions or measures; a Metric View requires at least one."
        if dropped_dims or dropped_measures:
            message += " Dropped during conversion:"
            if dropped_dims:
                message += " dimensions " + _sorted_names(dropped_dims)
            if dropped_measures:
                message += " measures " + _sorted_names(dropped_measures)
            message += " -- see the warnings for why each was dropped."
        raise ConversionError(message)

    if dimensions:
        view["dimensions"] = dimensions
    if measures:
        view["measures"] = measures
    if "parameters" in model_stash:
        view["parameters"] = model_stash["parameters"]
    if "materialization" in model_stash:
        view["materialization"] = model_stash["materialization"]

    _warn_dropped_model(model, notices)
    return view


def _schema_map_list(parent: dict[str, Any], key: str, scope: str) -> list[dict[str, Any]]:
    values = _schema_list(parent, key, scope)
    for index, element in enumerate(values):
        if not isinstance(element, dict):
            reason = f"{scope}: '{key}[{index}]' must be a mapping"
            raise ConversionError.invalid_input(reason, reason)
    return values


def _schema_list(parent: dict[str, Any], key: str, scope: str) -> list[Any]:
    # A present-but-null value (the bare `key:` idiom) counts as absent.
    value = parent.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        reason = f"{scope}: '{key}' must be a list"
        raise ConversionError.invalid_input(reason, reason)
    return value


# -- join tree -------------------------------------------------------------------


def _build_join_tree(
    model_name: str,
    datasets: dict[str, dict[str, Any]],
    relationships0: list[dict[str, Any]],
    fact_hint: str | None,
    notices: Notices,
) -> tuple[_Node, str]:
    for rel in relationships0:
        scope = (
            f"Model '{model_name}': relationship '"
            f"{jcat(rel.get('name')) if 'name' in rel else '<unnamed>'}'"
        )
        from_ = require(rel, "from", scope)
        to = require(rel, "to", scope)
        if jstr(from_) not in datasets or jstr(to) not in datasets:
            raise ConversionError(
                f"Model '{model_name}': relationship '{jcat(rel.get('name'))}' references an unknown dataset"
            )
        for key in ("from_columns", "to_columns"):
            if rel.get(key) is not None and not isinstance(rel.get(key), list):
                raise ConversionError(f"{scope}: from_columns and to_columns must be lists")
    relationships = [_orient_by_key(rel, datasets, notices) for rel in relationships0]
    if len(datasets) > MAX_JOIN_NODES:
        raise ConversionError(
            f"Model '{model_name}' has {len(datasets)} datasets; at most {MAX_JOIN_NODES} are supported."
        )
    fact = _pick_fact(model_name, datasets, relationships, fact_hint)
    _reject_directed_cycle(model_name, datasets, relationships)

    adjacency: dict[str, list[str]] = {name: [] for name in datasets}
    for rel in relationships:
        adjacency[jstr(rel.get("from"))].append(jstr(rel.get("to")))
        adjacency[jstr(rel.get("to"))].append(jstr(rel.get("from")))
    dist = {fact: 0}
    queue = deque([fact])
    while queue:
        current = queue.popleft()
        for neighbour in adjacency[current]:
            if neighbour not in dist:
                dist[neighbour] = dist[current] + 1
                queue.append(neighbour)
    unreachable = sorted(name for name in datasets if name not in dist)
    if unreachable:
        raise ConversionError(
            f"Model '{model_name}': datasets {jlist(unreachable)} are not reachable "
            f"from fact '{fact}' via relationships."
        )
    children_of: dict[str, list[tuple[str, dict[str, Any], bool]]] = {name: [] for name in datasets}
    for rel in relationships:
        a, b = jstr(rel.get("from")), jstr(rel.get("to"))
        if dist[a] == dist[b]:
            raise ConversionError(
                f"Model '{model_name}': relationship '{jcat(rel.get('name'))}' joins two datasets "
                "equidistant from the fact; the graph is not tree-shaped (it contains a cycle)."
            )
        parent, child = (a, b) if dist[a] < dist[b] else (b, a)
        children_of[parent].append((child, rel, parent == jstr(rel.get("from"))))
    counter = [0]
    root = _build(model_name, fact, {}, False, children_of, counter)
    return root, fact


def _build(
    model_name: str,
    dataset: str,
    rel: dict[str, Any],
    parent_is_from: bool,
    children_of: dict[str, list[tuple[str, dict[str, Any], bool]]],
    counter: list[int],
) -> _Node:
    counter[0] += 1
    if counter[0] > MAX_JOIN_NODES:
        raise ConversionError(
            f"Model '{model_name}': join graph fans out to more than {MAX_JOIN_NODES} joins; "
            "check for an unintended diamond explosion."
        )
    node = _Node(dataset, rel, parent_is_from)
    for child, child_rel, child_parent_is_from in children_of[dataset]:
        node.children.append(_build(model_name, child, child_rel, child_parent_is_from, children_of, counter))
    return node


def _assign_aliases(root: _Node, fact: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    _count_node(root, counts)
    _assign(root, None, fact, counts, {"source"})
    return counts


def _count_node(node: _Node, counts: dict[str, int]) -> None:
    counts[node.dataset] = counts.get(node.dataset, 0) + 1
    for child in node.children:
        _count_node(child, counts)


def _assign(node: _Node, parent_alias: str | None, fact: str, counts: dict[str, int], used: set[str]) -> None:
    if node.dataset == fact:
        alias = "source"
    else:
        if counts[node.dataset] == 1:
            base = node.dataset
        elif parent_alias is not None and parent_alias != "source":
            base = f"{parent_alias}_{node.dataset}"
        else:
            base = node.dataset
        alias = base
        n = 2
        while alias in used:
            alias = f"{base}_{n}"
            n += 1
    node.alias = alias
    used.add(alias)
    for child in node.children:
        _assign(child, alias, fact, counts, used)


def _pick_fact(
    model_name: str,
    datasets: dict[str, dict[str, Any]],
    relationships: list[dict[str, Any]],
    fact_hint: str | None,
) -> str:
    if fact_hint is not None:
        if fact_hint not in datasets:
            raise ConversionError(f"Model '{model_name}': requested source '{fact_hint}' is not a dataset")
        return fact_hint
    if len(datasets) > 1 and not relationships:
        raise ConversionError(
            f"Model '{model_name}': {len(datasets)} datasets but no relationships; "
            "cannot determine the fact table."
        )
    incoming = dict.fromkeys(datasets, 0)
    for rel in relationships:
        target = jstr(rel.get("to"))
        incoming[target] = incoming.get(target, 0) + 1
    roots = [name for name, count in incoming.items() if count == 0]
    if not roots:
        raise ConversionError(
            f"Model '{model_name}': join graph contains a cycle (no root dataset). "
            "A Metric View requires an acyclic, tree-shaped graph."
        )
    if len(roots) > 1:
        raise ConversionError(
            f"Model '{model_name}': multiple candidate fact datasets {jlist(sorted(roots))}. "
            "Name the grain with --source."
        )
    return roots[0]


def _mark_otm(model_name: str, root: _Node) -> None:
    """Mark each join with its branch's cardinality; reject a branch that mixes the two.

    Databricks requires every join within one top-level branch to share one
    cardinality, in either direction, and refuses a mixed view at CREATE.
    """
    for top in root.children:
        branch_is_otm = not top.parent_is_from
        top.is_otm = branch_is_otm
        _mark_otm_visit(model_name, top, branch_is_otm)


def _mark_otm_visit(model_name: str, node: _Node, branch_is_otm: bool) -> None:
    for child in node.children:
        is_otm = not child.parent_is_from
        if is_otm != branch_is_otm:
            raise ConversionError(
                f"Model '{model_name}': join '{child.alias}' is {_cardinality_name(is_otm)} but descends "
                f"from a {_cardinality_name(branch_is_otm)} join; a Metric View requires every join within "
                "one top-level branch to share the same cardinality."
            )
        child.is_otm = branch_is_otm
        _mark_otm_visit(model_name, child, branch_is_otm)


def _cardinality_name(is_otm: bool) -> str:
    return CARD_ONE_TO_MANY if is_otm else CARD_MANY_TO_ONE


def _reject_directed_cycle(
    model_name: str, datasets: dict[str, dict[str, Any]], relationships: list[dict[str, Any]]
) -> None:
    """Reject a directed cycle. A diamond is directed-acyclic and stays allowed."""
    out: dict[str, list[str]] = {name: [] for name in datasets}
    for rel in relationships:
        out[jstr(rel.get("from"))].append(jstr(rel.get("to")))
    done: set[str] = set()
    on_stack: dict[str, None] = {}  # ordered, like Java's LinkedHashSet
    for name in datasets:
        cycle = _find_cycle(name, out, done, on_stack)
        if cycle is not None:
            raise ConversionError(
                f"Model '{model_name}': relationships form a directed cycle {' -> '.join(cycle)}; "
                "a Metric View join graph must be acyclic."
            )


def _find_cycle(
    node: str, out: dict[str, list[str]], done: set[str], on_stack: dict[str, None]
) -> list[str] | None:
    if node in done:
        return None
    if node in on_stack:
        # Back-edge: report from the first occurrence so the message shows just the cycle.
        stack = list(on_stack)
        return [*stack[stack.index(node) :], node]
    on_stack[node] = None
    for successor in out[node]:
        cycle = _find_cycle(successor, out, done, on_stack)
        if cycle is not None:
            return cycle
    del on_stack[node]
    done.add(node)
    return None


def _qualify_measure(expr: str, dataset_head: re.Pattern[str] | None, dataset_alias_path: dict[str, str]) -> str:
    """Rewrite a ``<dataset>.`` qualifier to the alias path addressing it from the source."""
    return rewrite_qualifiers(expr, dataset_head, dataset_alias_path.get)


def _first_diamond_reference(
    expr: str, dataset_head: re.Pattern[str] | None, diamond_datasets: set[str]
) -> str | None:
    if dataset_head is None or not diamond_datasets:
        return None
    found: list[str] = []

    def detect(leaf: str) -> None:
        if not found and leaf in diamond_datasets:
            found.append(leaf)
        return None

    rewrite_qualifiers(expr, dataset_head, detect)
    return found[0] if found else None


def _sorted_names(names: dict[str | None, None]) -> str:
    return jlist(sorted(name for name in names if name is not None))


def _node_order(root: _Node) -> list[tuple[_Node, list[str]]]:
    order: list[tuple[_Node, list[str]]] = []
    _node_order_visit(root, [], order)
    return order


def _node_order_visit(node: _Node, path: list[str], order: list[tuple[_Node, list[str]]]) -> None:
    order.append((node, list(path)))
    for child in node.children:
        _node_order_visit(child, [*path, child.alias], order)


def _build_join(
    node: _Node, parent_alias: str, datasets: dict[str, dict[str, Any]], notices: Notices
) -> dict[str, Any]:
    rel = node.rel
    alias = node.alias
    join: dict[str, Any] = {"name": alias, "source": validate_source(datasets[node.dataset].get("source"), node.dataset)}

    stash = read_stash(rel, notices, f"join '{alias}'")
    from_cols = str_list(rel.get("from_columns"))
    to_cols = str_list(rel.get("to_columns"))
    if "on" in stash:
        # An import stashes the original `on` when rebuilding it from columns would not
        # reproduce it; restore it verbatim.
        join["on"] = stash["on"]
    else:
        _validate_join_columns(rel, from_cols, to_cols)
        parent_cols = from_cols if node.parent_is_from else to_cols
        child_cols = to_cols if node.parent_is_from else from_cols
        if parent_cols == child_cols:
            join["using"] = list(parent_cols)
        else:
            join["on"] = " AND ".join(
                f"{parent_alias}.{parent} = {alias}.{child}" for parent, child in zip(parent_cols, child_cols)
            )
    if "rely" in stash:
        join["rely"] = stash["rely"]
    elif node.parent_is_from and _covers_unique_key(datasets[node.dataset], to_cols):
        join["rely"] = {"at_most_one_match": True}
    if "cardinality" in stash:
        join["cardinality"] = stash["cardinality"]
    elif not node.parent_is_from:
        join["cardinality"] = CARD_ONE_TO_MANY
    nested = [_build_join(child, alias, datasets, notices) for child in node.children]
    if nested:
        join["joins"] = nested
    return join


def _covers_unique_key(dataset: dict[str, Any], join_cols: list[str]) -> bool:
    cols = set(join_cols)
    keys = []
    primary = str_list(dataset.get("primary_key"))
    if primary:
        keys.append(primary)
    keys += [str_list(key) for key in as_list(dataset.get("unique_keys"))]
    return any(key and cols.issuperset(key) for key in keys)


def _orient_by_key(rel: dict[str, Any], datasets: dict[str, dict[str, Any]], notices: Notices) -> dict[str, Any]:
    from_cols = str_list(rel.get("from_columns"))
    to_cols = str_list(rel.get("to_columns"))
    if not from_cols or not to_cols:
        return rel
    to_ds = datasets[jstr(rel.get("to"))]
    to_has_keys = bool(str_list(to_ds.get("primary_key"))) or bool(as_list(to_ds.get("unique_keys")))
    from_covers = _covers_unique_key(datasets[jstr(rel.get("from"))], from_cols)
    scope = f"relationship '{jcat(rel.get('name'))}'"
    if from_covers and to_has_keys and not _covers_unique_key(to_ds, to_cols):
        notices.warn(
            scope,
            "from/to looks mislabeled (the `from` columns are a declared key, the `to` columns "
            "are not); re-orienting so the key side is the `to`/one side",
        )
        swapped = dict(rel)
        swapped.update({"from": rel.get("to"), "to": rel.get("from"), "from_columns": to_cols, "to_columns": from_cols})
        return swapped
    if from_covers and not to_has_keys:
        notices.warn(
            scope,
            "the `from` columns are a declared key but the `to` side declares none, so from/to "
            "orientation can't be verified; using it as-is -- check the join direction if "
            "the resulting cardinality looks inverted",
        )
    return rel


def _validate_join_columns(rel: dict[str, Any], from_cols: list[str], to_cols: list[str]) -> None:
    name = jcat(rel.get("name"))
    if not from_cols or not to_cols:
        raise ConversionError(f"Relationship '{name}': from_columns and to_columns are required")
    if len(from_cols) != len(to_cols):
        raise ConversionError(
            f"Relationship '{name}': from_columns ({len(from_cols)}) and to_columns "
            f"({len(to_cols)}) must have the same length"
        )


# -- fields and metrics ----------------------------------------------------------


def _convert_field(
    field_: dict[str, Any], name0: str, qualifier: str, is_fact: bool, prefix: str | None, notices: Notices
) -> dict[str, Any] | None:
    scope = f"field '{name0}'"
    expr = pick_expression(field_.get("expression"), scope)
    if expr is None:
        notices.warn(scope, "no DATABRICKS/ANSI_SQL/OSSIE_SQL_2026 dialect; dropping field")
        return None
    if not is_fact:
        if is_simple_identifier(expr):
            expr = f"{qualifier}.{expr}"
        elif prefix is not None:
            notices.warn(
                scope, "complex expression on a fanned-out (diamond) join cannot be unambiguously qualified; dropped"
            )
            return None
        else:
            notices.warn(scope, "complex expression on a joined table; emitted as-is, verify qualification")
    dim: dict[str, Any] = {"name": f"{prefix}_{name0}" if prefix is not None else name0, "expr": expr}
    comment = merge_description(field_.get("description"), field_.get("ai_context"))
    if truthy(comment):
        dim["comment"] = comment
    label = jstr(field_.get("label"))
    if truthy(label):
        dim["display_name"] = label
    synonyms = synonyms_of(field_.get("ai_context"))
    if synonyms:
        dim["synonyms"] = _truncate_synonyms(synonyms, scope, notices)
    stash = read_stash(field_, notices, scope)
    if "format" in stash:
        dim["format"] = stash["format"]
    _warn_dropped_field(field_, scope, notices)
    return dim


def _convert_metric(
    metric: dict[str, Any],
    fact_qualifier: re.Pattern[str],
    dataset_head: re.Pattern[str] | None,
    dataset_alias_path: dict[str, str],
    diamond_datasets: set[str],
    seen_names: set[str],
    notices: Notices,
) -> dict[str, Any] | None:
    name = require_str(metric, "name", "metric")
    scope = f"metric '{name}'"
    if name.lower() in seen_names:
        raise ConversionError(
            f"metric '{name}' collides with another dimension/measure; "
            "Metric Views require unique dimension/measure names -- rename before use"
        )
    seen_names.add(name.lower())
    expr = pick_expression(metric.get("expression"), scope)
    if expr is None:
        notices.warn(scope, "no DATABRICKS/ANSI_SQL/OSSIE_SQL_2026 dialect; dropping metric")
        return None
    diamond = _first_diamond_reference(expr, dataset_head, diamond_datasets)
    if diamond is not None:
        notices.warn(
            scope,
            f"expression references dataset '{diamond}', reached by more than one join path "
            "(diamond); cannot be unambiguously qualified; dropped",
        )
        return None
    # A nested join column is addressed by its full path (`parent.nested.col`); a bare
    # nested alias would be read as struct access and fail silently.
    expr = _qualify_measure(expr, dataset_head, dataset_alias_path)
    # Fact columns are bare in measures (the Metric View idiom) -- outside literals only.
    expr = replace_outside_literals(expr, fact_qualifier, "")
    measure: dict[str, Any] = {"name": name, "expr": expr}
    comment = merge_description(metric.get("description"), metric.get("ai_context"))
    if truthy(comment):
        measure["comment"] = comment
    synonyms = synonyms_of(metric.get("ai_context"))
    if synonyms:
        measure["synonyms"] = _truncate_synonyms(synonyms, scope, notices)
    stash = read_stash(metric, notices, scope)
    for key in ("format", "window", "partition", "display_name"):
        # display_name rides in the stash: the Ossie metric has no `label`.
        if key in stash:
            measure[key] = stash[key]
    _warn_dropped_column(metric, scope, notices)
    return measure


class _CascadeDropper:
    """Drop every dimension/measure that references a dropped one, transitively.

    Worklists keep the Java phase order -- dimensions then measures, each in
    source order, a dependency found behind its source position deferred to the
    next round -- so output and notice order match the Java implementation.
    """

    SEED, DIMENSION, MEASURE = 0, 1, 2

    def __init__(self, dimensions, measures, dropped_dims, dropped_measures, notices):
        self.dimensions = dimensions
        self.measures = measures
        self.dropped_dims = dropped_dims
        self.dropped_measures = dropped_measures
        self.notices = notices
        self.removed_dims: set[int] = set()
        self.removed_measures: set[int] = set()
        self.patterns: dict[tuple[str, bool], re.Pattern[str]] = {}
        self.dims_now: set[int] = set()
        self.dims_next: set[int] = set()
        self.measures_now: set[int] = set()
        self.measures_next: set[int] = set()

    def run(self) -> None:
        for name in list(self.dropped_measures):
            self._propagate(name, True, self.SEED, -1)
        for name in list(self.dropped_dims):
            self._propagate(name, False, self.SEED, -1)
        while self.dims_now or self.measures_now:
            self._process(measure=False)
            self._process(measure=True)
            self.dims_now, self.dims_next = self.dims_next, set()
            self.measures_now, self.measures_next = self.measures_next, set()
        self.dimensions[:] = [c for i, c in enumerate(self.dimensions) if i not in self.removed_dims]
        self.measures[:] = [c for i, c in enumerate(self.measures) if i not in self.removed_measures]

    def _process(self, *, measure: bool) -> None:
        # Like BitSet.nextSetBit: indices scheduled ahead of the cursor during the
        # pass are still visited in this pass.
        now = self.measures_now if measure else self.dims_now
        phase = self.MEASURE if measure else self.DIMENSION
        index = -1
        while True:
            pending = [i for i in now if i > index]
            if not pending:
                break
            index = min(pending)
            now.discard(index)
            self._drop(index, measure, phase)

    def _drop(self, index: int, measure: bool, phase: int) -> None:
        removed = self.removed_measures if measure else self.removed_dims
        if index in removed:
            return
        column = (self.measures if measure else self.dimensions)[index]
        name = column["name"]
        reference = self._references_dropped(column["expr"], name, measure)
        if reference is None:
            return
        removed.add(index)
        kind = "measure" if measure else "dimension"
        self.notices.warn(
            f"{kind} '{name}'", f"references dropped '{reference}'; dropping (downstream of a dropped field/metric)"
        )
        dropped = self.dropped_measures if measure else self.dropped_dims
        if name not in dropped:
            dropped[name] = None
            self._propagate(name, measure, phase, index)

    def _propagate(self, name: str | None, measure_reference: bool, phase: int, source_index: int) -> None:
        if name is None:
            return
        pattern = self._reference_pattern(name, measure_reference)
        for index, column in enumerate(self.dimensions):
            if not self._is_pending(index, False) and self._matches(column, name, measure_reference, False, pattern):
                self._schedule(index, False, phase, source_index)
        for index, column in enumerate(self.measures):
            if not self._is_pending(index, True) and self._matches(column, name, measure_reference, True, pattern):
                self._schedule(index, True, phase, source_index)

    def _is_pending(self, index: int, measure: bool) -> bool:
        if measure:
            return index in self.removed_measures or index in self.measures_now or index in self.measures_next
        return index in self.removed_dims or index in self.dims_now or index in self.dims_next

    @staticmethod
    def _is_self_reference(column_is_measure: bool, dropped_is_measure: bool, column_name: str, dropped: str) -> bool:
        # Only within its own kind, case-insensitively: a measure sharing a dropped
        # dimension's name is a different object and still cascades.
        return column_is_measure == dropped_is_measure and dropped.lower() == column_name.lower()

    def _matches(self, column, reference, measure_reference, column_is_measure, pattern) -> bool:
        if self._is_self_reference(column_is_measure, measure_reference, column["name"], reference):
            return False
        expr = column["expr"]
        return reference.lower() in expr.lower() and find_outside_literals(expr, pattern)

    def _schedule(self, index: int, measure: bool, phase: int, source_index: int) -> None:
        if measure:
            if phase != self.MEASURE or index > source_index:
                self.measures_now.add(index)
            else:
                self.measures_next.add(index)
        elif phase == self.SEED or (phase == self.DIMENSION and index > source_index):
            self.dims_now.add(index)
        else:
            self.dims_next.add(index)

    def _references_dropped(self, expr: str, self_name: str, self_is_measure: bool) -> str | None:
        lower = expr.lower()
        for dropped, is_measure in [(n, True) for n in self.dropped_measures] + [(n, False) for n in self.dropped_dims]:
            if dropped is None or self._is_self_reference(self_is_measure, is_measure, self_name, dropped):
                continue
            if dropped.lower() in lower and find_outside_literals(expr, self._reference_pattern(dropped, is_measure)):
                return dropped
        return None

    def _reference_pattern(self, name: str, measure: bool) -> re.Pattern[str]:
        key = (name, measure)
        if key not in self.patterns:
            # A dimension is a bare token; the trailing (?!\s*\() excludes a same-spelled
            # function call such as YEAR(...).
            source = (
                rf"measure\(\s*{re.escape(name)}\s*\)"
                if measure
                else rf"(?<![\w.]){re.escape(name)}(?![\w.])(?!\s*\()"
            )
            self.patterns[key] = re.compile(source, re.IGNORECASE | re.ASCII)
        return self.patterns[key]


def _truncate_synonyms(synonyms: list[str], scope: str, notices: Notices) -> list[str]:
    if len(synonyms) > SYNONYM_LIMIT:
        notices.warn(scope, f"{len(synonyms)} synonyms exceeds Metric View limit; keeping first {SYNONYM_LIMIT}")
        return synonyms[:SYNONYM_LIMIT]
    return synonyms


# -- notices for dropped features ------------------------------------------------


def _warn_dropped_model(model: dict[str, Any], notices: Notices) -> None:
    if foreign_vendor_extensions(model):
        notices.warn("model", "foreign-vendor custom_extensions dropped")
    if truthy(model.get("ai_context")):
        notices.warn("model", "model-level ai_context dropped (only the description maps to the view comment)")
    for dataset in map(as_map, as_list(model.get("datasets"))):
        scope = f"dataset '{jcat(dataset.get('name'))}'"
        if str_list(dataset.get("primary_key")) or as_list(dataset.get("unique_keys")):
            notices.warn(
                scope,
                "primary_key/unique_keys not stored as columns; used to set "
                "rely.at_most_one_match on a matching many_to_one join where applicable",
            )
        if isinstance(dataset.get("ai_context"), dict) and dataset["ai_context"]:
            notices.warn(scope, "dataset-level ai_context (object) dropped")
        if truthy(dataset.get("description")):
            notices.warn(scope, "dataset-level description dropped (no per-source comment field)")
        if foreign_vendor_extensions(dataset):
            notices.warn(scope, "foreign-vendor custom_extensions dropped")
    for rel in map(as_map, as_list(model.get("relationships"))):
        scope = f"relationship '{jcat(rel.get('name')) if 'name' in rel else '<unnamed>'}'"
        if truthy(rel.get("ai_context")):
            notices.warn(scope, "relationship ai_context dropped")
        if foreign_vendor_extensions(rel):
            notices.warn(scope, "foreign-vendor custom_extensions dropped")


def _warn_dropped_field(field_: dict[str, Any], scope: str, notices: Notices) -> None:
    dimension = field_.get("dimension")
    if isinstance(dimension, dict) and "is_time" in dimension:
        notices.warn(scope, "dimension.is_time has no Metric View counterpart; dropped")
    _warn_dropped_column(field_, scope, notices)


def _warn_dropped_column(column: dict[str, Any], scope: str, notices: Notices) -> None:
    """Notices shared by fields and metrics: datatype, non-synonym ai_context, foreign extensions."""
    if column.get("datatype") is not None:
        notices.warn(scope, "datatype has no Metric View counterpart; dropped")
    ai_context = column.get("ai_context")
    if isinstance(ai_context, dict):
        dropped = sorted(jcat(key) for key in ai_context if key != "synonyms")
        if dropped:
            notices.warn(scope, f"ai_context {jlist(dropped)} dropped (only 'synonyms' has a Metric View counterpart)")
    if foreign_vendor_extensions(column):
        notices.warn(scope, "foreign-vendor custom_extensions dropped")
