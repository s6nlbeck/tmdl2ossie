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

"""Export-direction (Apache Ossie -> Databricks Metric View) tests for ``ossie_databricks``.

Ported from the Java converter's suite in
``vendor/ossie-converters-main/databricks/java/src/test/java/org/apache/ossie/converter/databricks/``
(``OssieConverterSuite.java``, ``OssieConverterRoundTripSuite.java``). Only tests that exercise
``convertOssieToMetricView`` (or ``parseYaml``) alone are ported; YAML inputs are kept verbatim.

Java tests NOT ported, grouped by reason:

Import direction (call ``convertMetricViewToOssie``, which is not being ported):
  OssieConverterSuite:
    measureDisplayNameSurvivesRoundTripViaStash, emptyModelNameFallsBackLikeAbsent,
    importDecomposesJoinIntoRelationship, exportRejectsMoreJoinsThanImportCanRebuild,
    exportAtTheJoinLimitStillSucceeds, nonEquiOnRejected, complexEquiOnRejected,
    equiJoinOnPreservedVerbatimWhenNonCanonical, equiJoinOnOverEqualColumnsStaysOnNotUsing,
    importRejectsCrossJoin, importRejectsUnsupportedVersion, mvToOssieToMvRoundTripsStash,
    fixtureBImportMatchesFixture, stashBlobUsesExpectedSpacing, importDropsEmptyOptionalFields,
    stashEscapesNonAsciiAsLowercaseHex, stashPreservesAValueContainingALiteralUnicodeEscape,
    joinOnTakesPrecedenceOverUsing, measureRewriteLeavesStringLiteralsAlone,
    nestedQualifiedMeasureRoundTripsBothWays, stashPreservesABackslashBeforeAUnicodeEscape,
    importValidatesAJoinSourceTheWayTheExportDoes, importAcceptsFieldsAsAliasForDimensions,
    importWarnsAndUsesDimensionsWhenBothDimensionsAndFieldsSet, importRejectsMalformedSource,
    importAcceptsAWithSubquerySource, importRejectsAJoinNamedSource,
    importRejectsADuplicateJoinName, importFlipsOneToManyJoinAndStashesTheGrain,
    importRecoversUniqueKeyFromAtMostOneMatch,
    importRejectsNonStringScalarsWhereStringsRequired,
    importLabelsEachExpressionWithItsMostPortableDialect,
    importKeepsWindowAndPartitionMeasuresDatabricks,
    importKeepsEveryMeasureOfAFilteredViewDatabricks, importKeepsNamesDefinedByTheViewDatabricks,
    importKeepsMeasureReferencesAndShadowedQualifiedColumnsDatabricks,
    importKeepsViewsWithUnseenDimensionNamesDatabricks,
    portableDialectSurvivesOssieToMetricViewToOssie,
    invalidMetricViewInputHasStructuredFailureDetails
  OssieConverterRoundTripSuite:
    metricViewRoundTripAcrossSeeds, ossieRoundTripAcrossSeeds (both need both directions)

Java-only internals:
  OssieConverterSuite:
    reverseDirectionSerializationFailuresAreInternalErrors (ThrowingBean, serializeOssie,
    writeStash); the dumpYaml half of publicYamlHelpersHaveStructuredFailureKinds (ThrowingBean
    serialization failure) -- the parseYaml half is ported below.

Whole suites skipped: ExpressionDialectSuite, OssieDatabricksConverterCliSuite.

No Java test exports ``fixtureB_ossie.yaml`` (fixtureB is only used for import), so there is no
fixtureB export comparison.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ossie_databricks import (
    MV_VERSION,  # noqa: F401  (part of the fixed API; kept imported so a missing name fails loudly)
    OSSIE_VERSION,
    ConversionError,
    convert_ossie_to_metric_view,
    parse_yaml,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "vendor/ossie-converters-main/databricks/java/src/test/resources"

# Mirrors OssieConverterCommon.MAX_JOIN_NODES (package-private in Java, not part of the Python API).
MAX_JOIN_NODES = 200


# -- helpers (ported from OssieConverterSuite) --------------------------------


def export(osi: str, source: str | None):
    return parse_yaml(convert_ossie_to_metric_view(osi, source).yaml)


def ossie_model_with_body(body: str) -> str:
    # Flat model at the document root. Callers pass a body indented two spaces (it used to sit
    # under a `- name: m` list item); de-indent it so it sits at the root next to `name`.
    lines = [line[2:] if line.startswith("  ") else line for line in body.split("\n")]
    return "version: '" + OSSIE_VERSION + "'\nname: m\n" + "\n".join(lines)


def cascade_notice(kind: str, name: str, reference: str) -> str:
    return (
        "[" + kind + " '" + name + "'] references dropped '" + reference
        + "'; dropping (downstream of a dropped field/metric)"
    )


def assert_invalid_ossie_input(yaml_text: str, expected_reason: str) -> None:
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(yaml_text, None)
    e = excinfo.value
    assert e.kind == "INVALID_INPUT"
    assert e.reason == expected_reason


def load_fixture(name: str) -> str:
    path = FIXTURES / ("ossie_" + name)
    if not path.is_file():
        raise FileNotFoundError("fixture not found: ossie_" + name)
    return path.read_text(encoding="utf-8")


def nested_join_model(metric_expr: str) -> str:
    """A fact with `customer` joined to it and `region` nested under `customer`, plus one metric."""
    return (
        "version: \"0.2.0.dev0\"\n"
        "name: sales\n"
        "datasets:\n"
        "  - name: lineitem\n"
        "    source: cat.sch.lineitem\n"
        "    fields:\n"
        "      - name: l_key\n"
        "        expression:\n"
        "          dialects: [{dialect: DATABRICKS, expression: l_key}]\n"
        "  - name: customer\n"
        "    source: cat.sch.customer\n"
        "    primary_key: [c_key]\n"
        "  - name: region\n"
        "    source: cat.sch.region\n"
        "    primary_key: [r_key]\n"
        "relationships:\n"
        "  - name: l_to_c\n"
        "    from: lineitem\n"
        "    to: customer\n"
        "    from_columns: [c_key]\n"
        "    to_columns: [c_key]\n"
        "  - name: c_to_r\n"
        "    from: customer\n"
        "    to: region\n"
        "    from_columns: [r_key]\n"
        "    to_columns: [r_key]\n"
        "metrics:\n"
        "  - name: pop\n"
        "    expression:\n"
        "      dialects: [{dialect: DATABRICKS, expression: \"" + metric_expr + "\"}]\n"
    )


def first_measure_expr(view) -> str:
    measures = view.get("measures")
    assert not (measures is None or len(measures) == 0), "expected one measure, got: " + str(view)
    return measures[0]["expr"]


# -- export direction ---------------------------------------------------------


def test_fixture_a_star_schema_exports_to_expected_metric_view():
    osi = (
        "version: \"0.2.0.dev0\"\n"
        "name: sales\n"
        "description: Sales orders with customer attributes\n"
        "datasets:\n"
        "  - name: orders\n"
        "    source: samples.tpch.orders\n"
        "    primary_key: [o_orderkey]\n"
        "    description: One row per order\n"
        "    fields:\n"
        "      - name: o_orderkey\n"
        "        expression:\n"
        "          dialects:\n"
        "            - dialect: DATABRICKS\n"
        "              expression: o_orderkey\n"
        "        description: Order identifier\n"
        "      - name: o_orderdate\n"
        "        expression:\n"
        "          dialects:\n"
        "            - dialect: DATABRICKS\n"
        "              expression: o_orderdate\n"
        "        label: Order Date\n"
        "        ai_context:\n"
        "          synonyms: [order date, date]\n"
        "  - name: customer\n"
        "    source: samples.tpch.customer\n"
        "    primary_key: [c_custkey]\n"
        "    fields:\n"
        "      - name: c_name\n"
        "        expression:\n"
        "          dialects:\n"
        "            - dialect: DATABRICKS\n"
        "              expression: c_name\n"
        "        description: Customer name\n"
        "relationships:\n"
        "  - name: orders_to_customer\n"
        "    from: orders\n"
        "    to: customer\n"
        "    from_columns: [o_custkey]\n"
        "    to_columns: [c_custkey]\n"
        "metrics:\n"
        "  - name: total_revenue\n"
        "    expression:\n"
        "      dialects:\n"
        "        - dialect: DATABRICKS\n"
        "          expression: SUM(o_totalprice)\n"
        "    description: Total order revenue\n"
        "    ai_context:\n"
        "      synonyms: [revenue, total revenue, sales]\n"
        "  - name: order_count\n"
        "    expression:\n"
        "      dialects:\n"
        "        - dialect: DATABRICKS\n"
        "          expression: COUNT(*)\n"
        "    description: Number of orders\n"
    )

    expected = (
        "version: '1.1'\n"
        "source: samples.tpch.orders\n"
        "comment: Sales orders with customer attributes\n"
        "joins:\n"
        "- name: customer\n"
        "  source: samples.tpch.customer\n"
        "  on: source.o_custkey = customer.c_custkey\n"
        "  rely:\n"
        "    at_most_one_match: true\n"
        "dimensions:\n"
        "- name: o_orderkey\n"
        "  expr: o_orderkey\n"
        "  comment: Order identifier\n"
        "- name: o_orderdate\n"
        "  expr: o_orderdate\n"
        "  display_name: Order Date\n"
        "  synonyms:\n"
        "  - order date\n"
        "  - date\n"
        "- name: c_name\n"
        "  expr: customer.c_name\n"
        "  comment: Customer name\n"
        "measures:\n"
        "- name: total_revenue\n"
        "  expr: SUM(o_totalprice)\n"
        "  comment: Total order revenue\n"
        "  synonyms:\n"
        "  - revenue\n"
        "  - total revenue\n"
        "  - sales\n"
        "- name: order_count\n"
        "  expr: COUNT(*)\n"
        "  comment: Number of orders\n"
    )

    assert parse_yaml(expected) == export(osi, None)


def test_unsupported_version_is_rejected():
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view("version: '9.9'\nname: m\n", None)
    assert "Unsupported Apache Ossie version" in str(excinfo.value)


def test_multiple_candidate_facts_without_source_is_rejected():
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- {name: orders, source: c.s.orders}\n"
        "- {name: returns, source: c.s.returns}\n"
        "- {name: customer, source: c.s.customer, primary_key: [c_custkey]}\n"
        "relationships:\n"
        "- {name: oc, from: orders, to: customer, from_columns: [o_custkey], to_columns: [c_custkey]}\n"
        "- {name: rc, from: returns, to: customer, from_columns: [re_custkey], to_columns: [c_custkey]}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    assert "multiple candidate fact datasets" in str(excinfo.value)


def test_one_to_many_emits_cardinality():
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: orders\n"
        "  source: c.s.orders\n"
        "  primary_key: [o_orderkey]\n"
        "  fields:\n"
        "  - {name: o_orderstatus, expression: {dialects: [{dialect: DATABRICKS, expression: o_orderstatus}]}}\n"
        "- name: lineitem\n"
        "  source: c.s.lineitem\n"
        "relationships:\n"
        "- {name: lio, from: lineitem, to: orders, from_columns: [l_orderkey], to_columns: [o_orderkey]}\n"
        "metrics:\n"
        "- {name: qty, expression: {dialects: [{dialect: DATABRICKS, expression: SUM(lineitem.l_quantity)}]}}\n"
    )
    view = export(osi, "orders")
    joins = view["joins"]
    join = joins[0]
    assert join.get("cardinality") == "one_to_many"


def test_one_to_many_nested_under_many_to_one_is_rejected():
    # A Metric View requires one cardinality per top-level branch, so a one-to-many join nested
    # under a many-to-one parent is rejected by Databricks just as the reverse nesting is. Fact
    # `d`; `f -> d` points at d (many-to-one from d's perspective), and `g -> f` points away from
    # f (one-to-many), which would nest one_to_many inside the many-to-one branch.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: d\n"
        "  source: c.s.d\n"
        "  fields:\n"
        "  - {name: dcol, expression: {dialects: [{dialect: DATABRICKS, expression: dcol}]}}\n"
        "- name: f\n"
        "  source: c.s.f\n"
        "  primary_key: [fk]\n"
        "- name: g\n"
        "  source: c.s.g\n"
        "relationships:\n"
        "- {name: df, from: d, to: f, from_columns: [fk], to_columns: [fk]}\n"
        "- {name: gf, from: g, to: f, from_columns: [fk], to_columns: [fk]}\n"
        "metrics:\n"
        "- {name: c, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(1)}]}}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, "d")
    message = str(excinfo.value)
    assert "share the same cardinality" in message, (
        "expected a mixed-cardinality rejection, got: " + message
    )


def test_directed_cycle_with_no_equidistant_edge_is_rejected():
    # a -> b -> c -> d -> e -> b with fact `a`: every edge spans adjacent BFS levels, so the
    # equidistance heuristic sees nothing, and `a` has zero incoming edges so pickFact finds a
    # root. Without a real acyclicity check this expanded into duplicate join paths (`d` under
    # both `c` and `e`), fabricating a tree from a cyclic model.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: a\n"
        "  source: c.s.a\n"
        "  fields:\n"
        "  - {name: acol, expression: {dialects: [{dialect: DATABRICKS, expression: acol}]}}\n"
        "- name: b\n"
        "  source: c.s.b\n"
        "- name: c\n"
        "  source: c.s.c\n"
        "- name: d\n"
        "  source: c.s.d\n"
        "- name: e\n"
        "  source: c.s.e\n"
        "relationships:\n"
        "- {name: ab, from: a, to: b, from_columns: [k], to_columns: [k]}\n"
        "- {name: bc, from: b, to: c, from_columns: [k], to_columns: [k]}\n"
        "- {name: cd, from: c, to: d, from_columns: [k], to_columns: [k]}\n"
        "- {name: de, from: d, to: e, from_columns: [k], to_columns: [k]}\n"
        "- {name: eb, from: e, to: b, from_columns: [k], to_columns: [k]}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, "a")
    message = str(excinfo.value)
    assert "directed cycle" in message, "expected a directed-cycle rejection, got: " + message


def test_diamond_is_still_accepted_by_the_cycle_check():
    # A diamond (`a -> b -> d` plus `a -> c -> d`) is an UNDIRECTED cycle but directed-acyclic, and
    # is supported via the fan-out aliases. The cycle check must not reject it.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: a\n"
        "  source: c.s.a\n"
        "- name: b\n"
        "  source: c.s.b\n"
        "- name: c\n"
        "  source: c.s.c\n"
        "- name: d\n"
        "  source: c.s.d\n"
        "  fields:\n"
        "  - {name: dcol, expression: {dialects: [{dialect: DATABRICKS, expression: dcol}]}}\n"
        "relationships:\n"
        "- {name: ab, from: a, to: b, from_columns: [k], to_columns: [k]}\n"
        "- {name: ac, from: a, to: c, from_columns: [k], to_columns: [k]}\n"
        "- {name: bd, from: b, to: d, from_columns: [k], to_columns: [k]}\n"
        "- {name: cd, from: c, to: d, from_columns: [k], to_columns: [k]}\n"
    )
    view = export(osi, "a")
    assert view.get("source") == "c.s.a"
    # `d` is reached by two paths, so its column is emitted once per fan-out alias.
    dims = view["dimensions"]
    assert len(dims) == 2, "diamond should fan out to one dimension per path, got: " + str(dims)


def test_measure_naming_a_diamond_dataset_is_dropped_with_notice():
    # Same diamond as above (`d` reached via `a -> b -> d` and `a -> c -> d`), plus a measure that
    # names `d` by its bare dataset name. A measure addresses a dataset by name, so it cannot be
    # split per fan-out path the way the dimension on `d` is; it is dropped with a notice rather
    # than silently bound to one arbitrary branch (as the dimension complex-expression path does).
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: a\n"
        "  source: c.s.a\n"
        "- name: b\n"
        "  source: c.s.b\n"
        "- name: c\n"
        "  source: c.s.c\n"
        "- name: d\n"
        "  source: c.s.d\n"
        "  fields:\n"
        "  - {name: dcol, expression: {dialects: [{dialect: DATABRICKS, expression: dcol}]}}\n"
        "relationships:\n"
        "- {name: ab, from: a, to: b, from_columns: [k], to_columns: [k]}\n"
        "- {name: ac, from: a, to: c, from_columns: [k], to_columns: [k]}\n"
        "- {name: bd, from: b, to: d, from_columns: [k], to_columns: [k]}\n"
        "- {name: cd, from: c, to: d, from_columns: [k], to_columns: [k]}\n"
        "metrics:\n"
        "- {name: dsum, expression: {dialects: [{dialect: DATABRICKS, expression: SUM(d.dcol)}]}}\n"
    )
    result = convert_ossie_to_metric_view(osi, "a")
    view = parse_yaml(result.yaml)
    # The diamond dimension on `d` still fans out to one dimension per path.
    assert len(view["dimensions"]) == 2
    # The measure that names `d` is dropped rather than bound to an arbitrary branch.
    assert "measures" not in view, (
        "a measure naming a diamond dataset must be dropped, got: " + str(view.get("measures"))
    )
    expected = (
        "expression references dataset 'd', reached by more than one join path "
        "(diamond); cannot be unambiguously qualified; dropped"
    )
    assert any(expected in n for n in result.notices), str(result.notices)


def test_nested_join_column_in_measure_gets_full_alias_path():
    # orders -> customer -> nation: `nation` nests under `customer`, so its columns are addressed
    # as `customer.nation.col`. A bare `nation.` head would be read as struct access on a
    # parameter, so the measure must be re-qualified -- and identically to the dimension path,
    # which already emits `customer.nation.population` for the same column.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: orders\n"
        "  source: c.s.orders\n"
        "- name: customer\n"
        "  source: c.s.customer\n"
        "  primary_key: [c_custkey]\n"
        "- name: nation\n"
        "  source: c.s.nation\n"
        "  primary_key: [n_nationkey]\n"
        "  fields:\n"
        "  - {name: population, expression: {dialects: [{dialect: DATABRICKS, expression: population}]}}\n"
        "relationships:\n"
        "- {name: oc, from: orders, to: customer, from_columns: [c_custkey], to_columns: [c_custkey]}\n"
        "- {name: cn, from: customer, to: nation, from_columns: [n_nationkey], to_columns: [n_nationkey]}\n"
        "metrics:\n"
        "- {name: pop, expression: {dialects: [{dialect: DATABRICKS, expression: SUM(nation.population)}]}}\n"
    )
    view = export(osi, "orders")

    dims = view["dimensions"]
    dim = dims[0]
    assert dim.get("expr") == "customer.nation.population", (
        "dimension path should qualify with the full alias path"
    )

    measures = view["measures"]
    measure = measures[0]
    assert measure.get("expr") == "SUM(customer.nation.population)", (
        "measure must use the same full alias path as the dimension, not a bare nested alias"
    )


def test_model_with_no_fields_or_metrics_is_rejected():
    # A Metric View requires at least one dimension or measure, so emitting a version+source-only
    # view would just fail at CREATE. Fail at conversion time instead.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: d\n"
        "  source: c.s.d\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    message = str(excinfo.value)
    assert "no dimensions or measures" in message, (
        "expected an empty-view rejection, got: " + message
    )


def test_empty_after_drops_names_the_dropped_columns():
    # Here the input is non-empty but everything drops: the only metric has no DATABRICKS/ANSI_SQL
    # dialect. The error must name the dropped column so the cause is actionable, since after the
    # cascade the emptiness is a consequence of the drop rather than an empty input.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: d\n"
        "  source: c.s.d\n"
        "metrics:\n"
        "- {name: only_metric, expression: {dialects: [{dialect: SNOWFLAKE, expression: SUM(x)}]}}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    message = str(excinfo.value)
    assert "no dimensions or measures" in message, (
        "expected an empty-view rejection, got: " + message
    )
    assert "only_metric" in message, "the message must name the dropped column, got: " + message


def test_duplicate_dimension_name_is_rejected():
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: orders\n"
        "  source: c.s.orders\n"
        "  fields:\n"
        "  - {name: id, expression: {dialects: [{dialect: DATABRICKS, expression: id}]}}\n"
        "- name: customer\n"
        "  source: c.s.customer\n"
        "  fields:\n"
        "  - {name: id, expression: {dialects: [{dialect: DATABRICKS, expression: id}]}}\n"
        "relationships:\n"
        "- {name: r, from: orders, to: customer, from_columns: [cid], to_columns: [id]}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    assert "collides" in str(excinfo.value)


def test_foreign_vendor_extension_dropped_with_notice():
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "custom_extensions:\n"
        "- {vendor_name: SNOWFLAKE, data: '{}'}\n"
        "datasets:\n"
        "- name: orders\n"
        "  source: c.s.orders\n"
        "  fields:\n"
        "  - {name: s, expression: {dialects: [{dialect: DATABRICKS, expression: s}]}}\n"
        "metrics:\n"
        "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    r = convert_ossie_to_metric_view(osi, None)
    assert any("foreign-vendor custom_extensions dropped" in m for m in r.notices)


def test_datatype_dropped_with_notice():
    # `datatype` (a logical type on a field or metric) has no Metric View slot, where a column's
    # type is inferred from its expression, so it is dropped with a notice on both.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: f\n"
        "  source: c.s.f\n"
        "  fields:\n"
        "  - {name: d, datatype: String, expression: "
        "{dialects: [{dialect: DATABRICKS, expression: d}]}}\n"
        "metrics:\n"
        "- {name: n, datatype: Integer, expression: "
        "{dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    r = convert_ossie_to_metric_view(osi, None)
    assert "[field 'd'] datatype has no Metric View counterpart; dropped" in r.notices, str(
        r.notices
    )
    assert "[metric 'n'] datatype has no Metric View counterpart; dropped" in r.notices, str(
        r.notices
    )


def test_relationship_foreign_vendor_extension_dropped_with_notice():
    # A foreign-vendor custom_extensions on a relationship has no Metric View slot, so it is
    # dropped with a notice, matching the model/dataset/field levels.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: o\n"
        "  source: c.s.o\n"
        "  fields:\n"
        "  - {name: a, expression: {dialects: [{dialect: DATABRICKS, expression: a}]}}\n"
        "- {name: c, source: c.s.c, primary_key: [k]}\n"
        "relationships:\n"
        "- {name: oc, from: o, to: c, from_columns: [k], to_columns: [k], "
        "custom_extensions: [{vendor_name: SNOWFLAKE, data: '{}'}]}\n"
        "metrics:\n"
        "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    r = convert_ossie_to_metric_view(osi, None)
    assert "[relationship 'oc'] foreign-vendor custom_extensions dropped" in r.notices, str(
        r.notices
    )


def test_scalar_join_columns_rejected_as_must_be_lists():
    # from_columns/to_columns given as a scalar (not a list) raise a clear "must be lists" error
    # rather than the misleading "are required" (empty-list) or a character-count length error.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- {name: a, source: c.s.a, fields: "
        "[{name: x, expression: {dialects: [{dialect: DATABRICKS, expression: x}]}}]}\n"
        "- {name: b, source: c.s.b}\n"
        "relationships:\n"
        "- {name: ab, from: a, to: b, from_columns: cid, to_columns: id}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    assert "must be lists" in str(excinfo.value), str(excinfo.value)


def test_empty_explicit_source_falls_back_like_absent():
    # An empty --source (e.g. an unset shell variable) must be treated as absent and fall back to
    # the fact heuristic, not taken as a real override (which would fail as "requested source ''
    # is not a dataset").
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: orders\n"
        "  source: c.s.orders\n"
        "  fields:\n"
        "  - {name: amount, expression: {dialects: [{dialect: DATABRICKS, expression: amount}]}}\n"
        "- name: customer\n"
        "  source: c.s.customer\n"
        "  fields:\n"
        "  - {name: c_name, expression: {dialects: [{dialect: DATABRICKS, expression: c_name}]}}\n"
        "relationships:\n"
        "- {name: oc, from: orders, to: customer, from_columns: [c_custkey], to_columns: [c_custkey]}\n"
        "metrics:\n"
        "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    assert export(osi, None) == export(osi, "")


# -- fixture comparisons ------------------------------------------------------
# The fixtures under src/test/resources/ossie_*.yaml pin the expected outputs
# checked into apache/ossie. Asserting the output parses equal to them (structure
# + stash blob STRINGS) is the real "one behavior, two implementations" guarantee.


def test_fixture_a_export_matches_fixture():
    out = convert_ossie_to_metric_view(load_fixture("fixtureA_ossie.yaml"), None).yaml
    assert parse_yaml(load_fixture("fixtureA_metric_view.yaml")) == parse_yaml(out)


def test_tpcds_export_matches_fixture():
    out = convert_ossie_to_metric_view(load_fixture("tpcds_ossie.yaml"), None).yaml
    assert parse_yaml(load_fixture("tpcds_metric_view.yaml")) == parse_yaml(out)


# -- parity edge cases found in review round 3 --------------------------------


def test_pick_expression_falls_through_empty_databricks_to_ansi():
    # An empty DATABRICKS dialect must fall through to ANSI_SQL,
    # not be selected as the (empty) expression.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: f\n"
        "  source: c.s.f\n"
        "  fields:\n"
        "  - name: d\n"
        "    expression:\n"
        "      dialects:\n"
        "      - {dialect: DATABRICKS, expression: ''}\n"
        "      - {dialect: ANSI_SQL, expression: ansi_col}\n"
        "metrics:\n"
        "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    out = export(osi, None)
    dims = out["dimensions"]
    dim = dims[0]
    assert dim.get("expr") == "ansi_col"


def test_ossie_sql2026_dimension_exported_not_dropped():
    # A dimension expressed only in OSSIE_SQL_2026 (Apache Ossie's portable, ANSI-compatible
    # dialect) must be exported, not dropped (apache/ossie#442).
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: f\n"
        "  source: c.s.f\n"
        "  fields:\n"
        "  - {name: region, expression: {dialects: "
        "[{dialect: OSSIE_SQL_2026, expression: o_region}]}}\n"
        "metrics:\n"
        "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    out = export(osi, None)
    dims = out["dimensions"]
    dim = dims[0]
    assert dim.get("expr") == "o_region"


def test_ossie_sql2026_measure_exported_not_dropped():
    # A measure expressed only in OSSIE_SQL_2026 must be exported, not dropped (apache/ossie#442).
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: f\n"
        "  source: c.s.f\n"
        "  fields:\n"
        "  - {name: region, expression: {dialects: [{dialect: DATABRICKS, expression: region}]}}\n"
        "metrics:\n"
        "- {name: rev, expression: {dialects: "
        "[{dialect: OSSIE_SQL_2026, expression: SUM(amount)}]}}\n"
    )
    out = export(osi, None)
    measures = out["measures"]
    measure = measures[0]
    assert measure.get("expr") == "SUM(amount)"


def test_databricks_dialect_preferred_over_ossie_sql2026():
    # DATABRICKS wins over OSSIE_SQL_2026 when both are present.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: f\n"
        "  source: c.s.f\n"
        "  fields:\n"
        "  - name: region\n"
        "    expression:\n"
        "      dialects:\n"
        "      - {dialect: OSSIE_SQL_2026, expression: portable_region}\n"
        "      - {dialect: DATABRICKS, expression: dbx_region}\n"
        "metrics:\n"
        "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    out = export(osi, None)
    dims = out["dimensions"]
    dim = dims[0]
    assert dim.get("expr") == "dbx_region"


def test_unsupported_dialect_only_dropped_with_warning():
    # A field whose only dialect is unsupported (here SNOWFLAKE) is dropped and warns, guarding
    # the allowlist against silently accepting any dialect (apache/ossie#442).
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: f\n"
        "  source: c.s.f\n"
        "  fields:\n"
        "  - {name: kept, expression: {dialects: [{dialect: DATABRICKS, expression: kept}]}}\n"
        "  - {name: region, expression: {dialects: "
        "[{dialect: SNOWFLAKE, expression: o_region}]}}\n"
        "metrics:\n"
        "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    notices = convert_ossie_to_metric_view(osi, None).notices
    assert any("no DATABRICKS/ANSI_SQL/OSSIE_SQL_2026 dialect" in n for n in notices), str(notices)


def test_pick_expression_rejects_non_string_expression():
    # A non-string dialect expression (e.g. a YAML number) must raise, not be coerced.
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: f\n"
        "  source: c.s.f\n"
        "  fields:\n"
        "  - name: d\n"
        "    expression:\n"
        "      dialects:\n"
        "      - {dialect: DATABRICKS, expression: 123}\n"
        "metrics:\n"
        "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    assert "expression must be a string" in str(excinfo.value)


def test_bare_on_off_values_stay_strings_not_booleans():
    # YAML 1.1 would read a bare `on`/`off`/`yes`/`no` value as a boolean, silently losing
    # a join condition or turning a synonym into `true`. Confirm the converter's parser
    # keeps them as strings (the reader uses YAML 1.2 boolean semantics).
    m = parse_yaml("a: on\nb: off\nc: yes\nd: no\n")
    assert m.get("a") == "on"
    assert m.get("b") == "off"
    assert m.get("c") == "yes"
    assert m.get("d") == "no"


def test_export_qualifies_a_nested_measure_with_the_full_join_path():
    # A Metric View addresses a nested join column by its full path, so a metric naming the
    # dataset must come out as `customer.region.population`.
    assert first_measure_expr(
        export(nested_join_model("SUM(region.population)"), None)
    ) == "SUM(customer.region.population)"


def test_export_does_not_qualify_a_measure_path_twice():
    # The rewrite used to run one dataset name at a time, so it matched the `region.` inside the
    # path it had just written and produced `SUM(customer.customer.region.population)` -- silently,
    # with no notice. An expression that already carries the full path (what an import leaves
    # behind) must come out unchanged.
    assert first_measure_expr(
        export(nested_join_model("SUM(customer.region.population)"), None)
    ) == "SUM(customer.region.population)"


def test_cascade_drop_ignores_a_dropped_name_inside_a_string_literal():
    # A field dropped for lacking a usable dialect must not take an unrelated measure with it just
    # because its name appears in a string literal: `'us'` is not a reference to a column `us`.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        "name: sales\n"
        "datasets:\n"
        "  - name: orders\n"
        "    source: cat.sch.orders\n"
        "    fields:\n"
        "      - name: amount\n"
        "        expression:\n"
        "          dialects: [{dialect: DATABRICKS, expression: amount}]\n"
        "      - name: us\n"
        "        expression:\n"
        "          dialects: [{dialect: SNOWFLAKE, expression: us_col}]\n"
        "metrics:\n"
        "  - name: amt_us\n"
        "    expression:\n"
        "      dialects:\n"
        "        - dialect: DATABRICKS\n"
        "          expression: \"SUM(IF(region = 'us', amount, 0))\"\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    assert first_measure_expr(parse_yaml(result.yaml)) == "SUM(IF(region = 'us', amount, 0))"
    for notice in result.notices:
        assert not ("amt_us" in notice and "references dropped" in notice), (
            "the measure only mentions 'us' in a literal, got notice: " + notice
        )


def test_cascade_drop_matches_a_dropped_name_case_insensitively():
    # Databricks SQL identifiers are case-insensitive. A metric that references a dropped field in a
    # different case (COUNT(DISTINCT REGION_NAME) over a dropped region_name) must cascade-drop
    # rather than survive as a dangling reference.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        "name: m\n"
        "datasets:\n"
        "  - name: d\n"
        "    source: cat.sch.t\n"
        "    fields:\n"
        "      - name: id\n"
        "        expression:\n"
        "          dialects: [{dialect: DATABRICKS, expression: id}]\n"
        "      - name: region_name\n"
        "        expression:\n"
        "          dialects: [{dialect: T_SQL, expression: region_name}]\n"
        "metrics:\n"
        "  - name: region_count\n"
        "    expression:\n"
        "      dialects: [{dialect: DATABRICKS, expression: COUNT(DISTINCT REGION_NAME)}]\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    view = parse_yaml(result.yaml)
    # The measure references the dropped field in upper case, so it is cascade-dropped.
    assert "measures" not in view, "region_count must cascade-drop, got: " + str(
        view.get("measures")
    )
    # The unrelated dimension survives.
    assert len(view["dimensions"]) == 1
    assert cascade_notice("measure", "region_count", "region_name") in result.notices, str(
        result.notices
    )


def test_cascade_drop_keeps_a_function_call_named_like_a_dropped_field():
    # A dropped field whose name collides with a SQL function token in a surviving expression must
    # not cascade-drop it: COUNT_IF(YEAR(order_date) = 2026) calls the YEAR function, it is not a
    # reference to a dropped `year` field. The reference match excludes a NAME(...) function call.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        "name: m\n"
        "datasets:\n"
        "  - name: d\n"
        "    source: cat.sch.t\n"
        "    fields:\n"
        "      - name: order_date\n"
        "        expression:\n"
        "          dialects: [{dialect: DATABRICKS, expression: order_date}]\n"
        "      - name: year\n"
        "        expression:\n"
        "          dialects: [{dialect: T_SQL, expression: year}]\n"
        "metrics:\n"
        "  - name: orders_2026\n"
        "    expression:\n"
        "      dialects: [{dialect: DATABRICKS, expression: COUNT_IF(YEAR(order_date) = 2026)}]\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    view = parse_yaml(result.yaml)
    measures = view["measures"]
    # YEAR(...) is a function call, not a reference to the dropped `year`, so the measure survives.
    assert len(measures) == 1, result.yaml
    assert measures[0].get("name") == "orders_2026"


def test_cascade_drop_keeps_a_survivor_differing_from_a_dropped_field_only_in_case():
    # A dropped field and a surviving dimension whose names differ only in case can coexist. The
    # survivor's bare self-reference (`region`) is its own name, not a reference to the dropped
    # `REGION`, so the self-guard is case-folded and the survivor is kept.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        "name: m\n"
        "datasets:\n"
        "  - name: d\n"
        "    source: cat.sch.t\n"
        "    fields:\n"
        "      - name: REGION\n"
        "        expression:\n"
        "          dialects: [{dialect: T_SQL, expression: REGION}]\n"
        "      - name: region\n"
        "        expression:\n"
        "          dialects: [{dialect: DATABRICKS, expression: region}]\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    view = parse_yaml(result.yaml)
    dims = view["dimensions"]
    assert len(dims) == 1, result.yaml
    assert dims[0].get("name") == "region"


def test_cascade_drop_drops_a_measure_named_like_a_dropped_dimension():
    # A measure that shares a dropped dimension's name -- same name, SAME case -- is not a
    # self-reference: the guard is scoped to the column's own kind, so the measure's reference to
    # the dropped dimension is detected and the measure is cascade-dropped rather than emitted
    # dangling. The same-case clash is what pins the kind-scoping: a case-only difference already
    # cascade-drops without it (the pre-fix guard was case-sensitive), so this must match case.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        "name: m\n"
        "datasets:\n"
        "  - name: d\n"
        "    source: cat.sch.t\n"
        "    fields:\n"
        "      - name: id\n"
        "        expression:\n"
        "          dialects: [{dialect: DATABRICKS, expression: id}]\n"
        "      - name: region\n"
        "        expression:\n"
        "          dialects: [{dialect: T_SQL, expression: region}]\n"
        "metrics:\n"
        "  - name: region\n"
        "    expression:\n"
        "      dialects: [{dialect: DATABRICKS, expression: SUM(region)}]\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    view = parse_yaml(result.yaml)
    assert "measures" not in view, "measure 'region' must cascade-drop, got: " + str(
        view.get("measures")
    )
    assert cascade_notice("measure", "region", "region") in result.notices, str(result.notices)


def test_export_warns_when_a_column_ai_context_object_is_dropped():
    # `ai_context` is `string | object`; only the object's `synonyms` has a Metric View slot, so
    # the other members are dropped -- and the README promises a notice when that happens. The
    # same goes for foreign-vendor extensions on a metric.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        "name: sales\n"
        "datasets:\n"
        "  - name: orders\n"
        "    source: cat.sch.orders\n"
        "    fields:\n"
        "      - name: amount\n"
        "        expression:\n"
        "          dialects: [{dialect: DATABRICKS, expression: amount}]\n"
        "        ai_context:\n"
        "          instructions: do not use this column\n"
        "          synonyms: [amt]\n"
        "metrics:\n"
        "  - name: total\n"
        "    expression:\n"
        "      dialects: [{dialect: DATABRICKS, expression: SUM(amount)}]\n"
        "    ai_context:\n"
        "      examples: [how much did we sell]\n"
        "    custom_extensions:\n"
        "      - vendor_name: SNOWFLAKE\n"
        "        data: \"{}\"\n"
    )
    notices = convert_ossie_to_metric_view(osi, None).notices
    assert (
        "[field 'amount'] ai_context [instructions] dropped "
        "(only 'synonyms' has a Metric View counterpart)"
    ) in notices, "expected a notice for the field's dropped ai_context members, got: " + str(
        notices
    )
    assert (
        "[metric 'total'] ai_context [examples] dropped "
        "(only 'synonyms' has a Metric View counterpart)"
    ) in notices, "expected a notice for the metric's dropped ai_context members, got: " + str(
        notices
    )
    assert "[metric 'total'] foreign-vendor custom_extensions dropped" in notices, (
        "expected a notice for the metric's foreign-vendor extensions, got: " + str(notices)
    )


# -- hardening: strict schema/stash parsing and structured failures -----------


def test_bare_null_optional_collections_convert_like_absent():
    # A present-but-null optional collection (the bare `relationships:` YAML idiom) must convert
    # like an absent key, as the base reader (asList) did. Only a scalar/map there is rejected.
    yaml_text = (
        "version: '" + OSSIE_VERSION + "'\n"
        "name: m\n"
        "datasets:\n"
        "- name: d\n"
        "  source: c.s.t\n"
        "  fields:\n"
        "  - name: region\n"
        "    expression: {dialects: [{dialect: DATABRICKS, expression: region}]}\n"
        "relationships:\n"
        "metrics:\n"
        "- name: cnt\n"
        "  expression: {dialects: [{dialect: DATABRICKS, expression: 'count(*)'}]}\n"
    )
    mv = convert_ossie_to_metric_view(yaml_text, None).yaml
    assert "cnt" in mv, "expected the model to convert, got: " + mv


def test_invalid_ossie_input_has_structured_failure_details():
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view("just a scalar", None)
    e = excinfo.value
    assert e.kind == "INVALID_INPUT"
    assert e.reason == "it is not a mapping at the root"


def test_malformed_schema_collections_are_rejected():
    assert_invalid_ossie_input(
        "version: '" + OSSIE_VERSION + "'\nsemantic_model:\n- name: m\n",
        "Legacy 'semantic_model' wrappers are not supported; place the model properties "
        "directly at the document root",
    )
    assert_invalid_ossie_input(
        "version: '" + OSSIE_VERSION + "'\ndatasets: []\n",
        "Apache Ossie model requires a string 'name' at the document root",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body("  datasets: nope\n"),
        "Model 'm': 'datasets' must be a list",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body("  datasets:\n  - nope\n"),
        "Model 'm': 'datasets[0]' must be a mapping",
    )

    dataset = (
        "  datasets:\n"
        "  - name: d\n"
        "    source: c.s.t\n"
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "  relationships: nope\n"),
        "Model 'm': 'relationships' must be a list",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "  relationships:\n  - nope\n"),
        "Model 'm': 'relationships[0]' must be a mapping",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "    fields: nope\n"),
        "Dataset 'd': 'fields' must be a list",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "    fields:\n    - nope\n"),
        "Dataset 'd': 'fields[0]' must be a mapping",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "  metrics: nope\n"),
        "Model 'm': 'metrics' must be a list",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "  metrics:\n  - nope\n"),
        "Model 'm': 'metrics[0]' must be a mapping",
    )


def test_malformed_expression_collections_are_rejected():
    dataset_prefix = (
        "  datasets:\n"
        "  - name: d\n"
        "    source: c.s.t\n"
        "    fields:\n"
        "    - name: id\n"
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset_prefix + "      expression: nope\n"),
        "field 'id': 'expression' must be a mapping",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset_prefix + "      expression: {dialects: nope}\n"),
        "field 'id': 'expression.dialects' must be a list",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset_prefix + "      expression: {dialects: [nope]}\n"),
        "field 'id': 'expression.dialects[0]' must be a mapping",
    )

    valid_field = (
        dataset_prefix
        + "      expression:\n"
        "        dialects:\n"
        "        - {dialect: DATABRICKS, expression: id}\n"
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(
            valid_field
            + "  metrics:\n"
            "  - name: count_id\n"
            "    expression: {dialects: nope}\n"
        ),
        "metric 'count_id': 'expression.dialects' must be a list",
    )


def test_malformed_custom_extensions_are_rejected():
    valid_model_body = (
        "  datasets:\n"
        "  - name: d\n"
        "    source: c.s.t\n"
        "    fields:\n"
        "    - name: id\n"
        "      expression:\n"
        "        dialects:\n"
        "        - {dialect: DATABRICKS, expression: id}\n"
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(
            "  custom_extensions: {vendor_name: DATABRICKS, data: '{}'}\n" + valid_model_body
        ),
        "'custom_extensions' must be a list",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body("  custom_extensions: [nope]\n" + valid_model_body),
        "'custom_extensions[0]' must be a mapping",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(
            "  custom_extensions:\n"
            "  - {vendor_name: DATABRICKS, data: 123}\n"
            + valid_model_body
        ),
        "DATABRICKS custom_extensions data must be a string",
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(
            "  custom_extensions:\n"
            "  - {vendor_name: DATABRICKS, data: '[]'}\n"
            + valid_model_body
        ),
        "DATABRICKS custom_extensions data must be a JSON object",
    )

    for data in [
        "filter: x = 1",
        "{\"filter\": \"x\", \"filter\": \"y\"}",
        "{\"filter\": \"x\"} true",
    ]:
        yaml_text = ossie_model_with_body(
            "  custom_extensions:\n"
            "  - vendor_name: DATABRICKS\n"
            "    data: '" + data + "'\n"
            + valid_model_body
        )
        with pytest.raises(ConversionError) as excinfo:
            convert_ossie_to_metric_view(yaml_text, None)
        e = excinfo.value
        assert e.kind == "INVALID_INPUT"
        assert e.reason.startswith("DATABRICKS custom_extensions data is not valid JSON:"), e.reason


def test_duplicate_databricks_stash_keeps_first_and_warns():
    # Two DATABRICKS custom_extensions entries on one object is malformed, but rather than fail we
    # keep the first, ignore the rest, and warn so the dropped entry is not lost silently.
    osi = ossie_model_with_body(
        "  custom_extensions:\n"
        "  - {vendor_name: DATABRICKS, data: '{\"filter\": \"a = 1\"}'}\n"
        "  - {vendor_name: DATABRICKS, data: '{\"filter\": \"b = 2\"}'}\n"
        "  datasets:\n"
        "  - name: d\n"
        "    source: c.s.t\n"
        "    fields:\n"
        "    - {name: id, expression: {dialects: [{dialect: DATABRICKS, expression: id}]}}\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    view = parse_yaml(result.yaml)
    assert view.get("filter") == "a = 1", "the first DATABRICKS entry must win"
    assert any(
        "more than one DATABRICKS custom_extensions entry" in n for n in result.notices
    ), str(result.notices)


def test_oversized_dataset_graph_is_rejected_before_recursive_traversal():
    body = ["  datasets:\n"]
    for i in range(MAX_JOIN_NODES + 1):
        body.append("  - {name: d" + str(i) + ", source: c.s.t" + str(i) + "}\n")
    body.append("  relationships:\n")
    for i in range(MAX_JOIN_NODES):
        body.append(
            "  - {name: r" + str(i)
            + ", from: d" + str(i)
            + ", to: d" + str(i + 1)
            + ", from_columns: [id], to_columns: [id]}\n"
        )

    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(ossie_model_with_body("".join(body)), None)

    assert "at most 200 are supported" in str(excinfo.value), str(excinfo.value)


def test_duplicate_yaml_keys_are_rejected_at_every_depth():
    top_level = (
        "version: '" + OSSIE_VERSION + "'\n"
        "version: '" + OSSIE_VERSION + "'\n"
        "name: m\n"
    )
    nested = ossie_model_with_body(
        "  datasets:\n"
        "  - name: d\n"
        "    source: c.s.first\n"
        "    source: c.s.second\n"
    )
    for yaml_text in [top_level, nested]:
        with pytest.raises(ConversionError) as excinfo:
            convert_ossie_to_metric_view(yaml_text, None)
        e = excinfo.value
        assert e.kind == "INVALID_INPUT"
        assert "duplicate key" in e.reason, e.reason


def test_reverse_ordered_drop_chain_is_propagated_without_repeated_full_scans():
    # Every field depends on the preceding field, but reverse source order means the old repeated
    # pass implementation could discover only one new drop per pass.
    chain_length = 400
    body = [
        "  datasets:\n"
        "  - name: d\n"
        "    source: c.s.d\n"
        "    fields:\n"
    ]
    for index in range(chain_length, 0, -1):
        body.append("    - name: d" + str(index) + "\n")
        body.append("      expression:\n")
        body.append("        dialects:\n")
        body.append("        - {dialect: DATABRICKS, expression: d" + str(index - 1) + "}\n")
    body.append("    - name: d0\n")
    body.append("      expression:\n")
    body.append("        dialects:\n")
    body.append("        - {dialect: SNOWFLAKE, expression: d0}\n")
    body.append("  metrics:\n")
    body.append("  - name: row_count\n")
    body.append("    expression:\n")
    body.append("      dialects:\n")
    body.append("      - {dialect: DATABRICKS, expression: count(*)}\n")

    result = convert_ossie_to_metric_view(ossie_model_with_body("".join(body)), None)
    view = parse_yaml(result.yaml)

    assert "dimensions" not in view, result.yaml
    assert len(view["measures"]) == 1, result.yaml
    assert len(result.notices) == chain_length + 1, str(result.notices)
    cascade_notices = [
        notice for notice in result.notices if "downstream of a dropped field/metric" in notice
    ]
    assert len(cascade_notices) == chain_length, str(cascade_notices)
    for index in range(1, chain_length + 1):
        assert cascade_notices[index - 1] == cascade_notice(
            "dimension", "d" + str(index), "d" + str(index - 1)
        )


def test_cascade_drop_preserves_dimension_then_measure_phase_order():
    osi = (
        "version: 0.2.0.dev0\n"
        "name: m\n"
        "datasets:\n"
        "- name: d\n"
        "  source: c.s.d\n"
        "  fields:\n"
        "    - {name: d1, expression: {dialects: "
        "[{dialect: DATABRICKS, expression: bad_dim}]}}\n"
        "    - {name: d0, expression: {dialects: "
        "[{dialect: DATABRICKS, expression: 'measure(m1)'}]}}\n"
        "    - {name: bad_dim, expression: {dialects: "
        "[{dialect: SNOWFLAKE, expression: bad_dim}]}}\n"
        "metrics:\n"
        "- {name: m1, expression: {dialects: "
        "[{dialect: DATABRICKS, expression: 'measure(bad_measure)'}]}}\n"
        "- {name: m0, expression: {dialects: "
        "[{dialect: DATABRICKS, expression: d0}]}}\n"
        "- {name: bad_measure, expression: {dialects: "
        "[{dialect: SNOWFLAKE, expression: bad_measure}]}}\n"
        "- {name: keep, expression: {dialects: "
        "[{dialect: DATABRICKS, expression: 'count(*)'}]}}\n"
    )

    result = convert_ossie_to_metric_view(osi, None)
    cascade_notices = [
        notice for notice in result.notices if "downstream of a dropped field/metric" in notice
    ]

    assert cascade_notices == [
        cascade_notice("dimension", "d1", "bad_dim"),
        cascade_notice("measure", "m1", "bad_measure"),
        cascade_notice("dimension", "d0", "m1"),
        cascade_notice("measure", "m0", "d0"),
    ]


def test_public_yaml_helpers_have_structured_failure_kinds():
    # NOTE: partial port. Only the parseYaml half is ported; the Java dumpYaml half feeds a
    # ThrowingBean (a getter that throws during Jackson serialization), which has no Python
    # equivalent. Java `getCause() != null` is mapped to an explicitly chained `__cause__`
    # (i.e. the implementation is expected to `raise ConversionError(...) from err`).
    with pytest.raises(ConversionError) as excinfo:
        parse_yaml("version: [1")
    parse_error = excinfo.value
    assert parse_error.kind == "INVALID_INPUT"
    assert str(parse_error) == parse_error.reason
    assert parse_error.__cause__ is not None
