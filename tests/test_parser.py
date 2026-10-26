"""Tests for the TMDL parser: the grammar layer (``helpers``) and the vocabulary layer (``parser``).

Most model-level tests run against ``tests/fixtures/Sales.SemanticModel``, a
synthetic model covering what the Retail sample lacks: measures (inline and
multi-line DAX), a calculated column, ``isKey``, SQL-backed partitions, a
hierarchy, quoted names, an explicit one-to-many and an inactive relationship.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tmdl_parser import TMDLParseError, parse_semantic_model, parse_table_file
from tmdl_parser.helpers import (
    find_delimiter,
    indent_of,
    parse_document,
    split_column_reference,
    unquote_name,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SALES = FIXTURES / "Sales.SemanticModel"
RETAIL = ROOT / "samples" / "Retail" / "pbi_file.SemanticModel"


@pytest.fixture(scope="module")
def sales():
    return parse_semantic_model(SALES)


# --------------------------------------------------------------------------- #
# Grammar: text-level helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "level"),
    [("x", 0), ("\t\tx", 2), ("        x", 2), ("\t    x", 2), ("   x", 0)],
)
def test_indent_of_counts_tabs_and_four_space_runs(raw, level):
    assert indent_of(raw) == level


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("formatString: hh:mm", (":", 12)),
        ("source = let", ("=", 7)),
        # A delimiter inside a quoted name does not count.
        ("measure 'Margin: net' = 1", ("=", 22)),
        ("isHidden", (None, -1)),
    ],
)
def test_find_delimiter_takes_the_first_unquoted_one(text, expected):
    assert find_delimiter(text) == expected


def test_unquote_name_undoes_doubled_quotes():
    assert unquote_name("'Product Details'") == "Product Details"
    assert unquote_name("'It''s'") == "It's"
    assert unquote_name("plain") == "plain"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("verkauefe.Datum", ("verkauefe", "Datum")),
        ("'Product Details'.ProductKey", ("Product Details", "ProductKey")),
        ("'a.b'.c", ("a.b", "c")),
        ("verkauefe[Datum]", ("verkauefe", "Datum")),
        ("'my table'[Datum]", ("my table", "Datum")),
        ("Datum", (None, "Datum")),
    ],
)
def test_split_column_reference_spellings(text, expected):
    assert split_column_reference(text) == expected


# --------------------------------------------------------------------------- #
# Grammar: block structure
# --------------------------------------------------------------------------- #


def test_description_attaches_to_the_next_block_only():
    root = parse_document(
        "/// The table.\n/// Second line.\ntable t\n\tlineageTag: x\n\tcolumn c\n\t\tdataType: string\n"
    )
    [table] = root.children
    assert table.description == "The table.\nSecond line."
    assert table.first("column").description is None


def test_lone_word_is_a_flag_unless_it_has_nested_content():
    root = parse_document("model Model\n\tisHidden\n\tdataAccessOptions\n\t\tlegacyRedirects\n")
    [model] = root.children
    assert model.flags == ["isHidden"]
    assert model.first("dataAccessOptions").flags == ["legacyRedirects"]


def test_multiline_dax_is_told_apart_from_the_property_after_it():
    text = "table t\n\tmeasure m =\n\t\t\tVAR x = 1\n\n\t\t\tRETURN\n\t\t\t\tx\n\t\tformatString: 0\n"
    measure = parse_document(text).children[0].first("measure")
    assert measure.expression == "VAR x = 1\n\nRETURN\n\tx"
    assert measure.properties == {"formatString": "0"}


def test_partition_header_assignment_is_the_source_type():
    text = (
        "table t\n\tpartition t = m\n\t\tmode: import\n\t\tsource =\n"
        "\t\t\t\tlet\n\t\t\t\t\tx = 1\n\t\t\t\tin\n\t\t\t\t\tx\n"
    )
    partition = parse_document(text).children[0].first("partition")
    assert partition.expression == "m"
    # The M keeps its relative indentation, minus the shared prefix.
    assert partition.properties["source"] == "let\n\tx = 1\nin\n\tx"


def test_annotations_and_comments():
    text = "table t\n\t// just a comment\n\tannotation PBI_QueryOrder = [\"a\",\"b\"]\n\tannotation 'Odd Key' = 1\n"
    [table] = parse_document(text).children
    assert table.annotations == {"PBI_QueryOrder": '["a","b"]', "Odd Key": "1"}
    assert table.properties == {}


def test_crlf_line_endings():
    [table] = parse_document("table t\r\n\tlineageTag: x\r\n").children
    assert table.properties == {"lineageTag": "x"}


def test_unexpected_indentation_reports_the_line():
    with pytest.raises(TMDLParseError, match=r"x\.tmdl:3: unexpected indentation"):
        parse_document("table t\n\tlineageTag: x\n\t\t\tstray\n", path="x.tmdl")


# --------------------------------------------------------------------------- #
# Vocabulary: a whole model
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "entry",
    [SALES / "definition", SALES, FIXTURES, SALES / "definition" / "model.tmdl"],
    ids=["definition", "SemanticModel", "project root", "model.tmdl"],
)
def test_accepts_every_entry_point(entry):
    assert parse_semantic_model(entry).source_path == SALES / "definition"


def test_missing_model_file(tmp_path):
    with pytest.raises(TMDLParseError, match="no model.tmdl found"):
        parse_semantic_model(tmp_path)


def test_tables_follow_the_ref_order_including_quoted_names(sales):
    assert sales.model.tableRefs == ["Sales", "Customer", "Region", "Product Details", "LocalDateTable_0001"]
    assert [table.name for table in sales.tables] == sales.model.tableRefs


def test_model_and_database(sales):
    model = sales.model
    assert model.description == "Order-line sales with customer, region and product dimensions."
    assert model.culture == "en-US"
    assert model.dataAccessOptions == ["legacyRedirects"]
    assert model.cultureRefs == ["en-US"]
    assert model.annotations["PBI_QueryOrder"] == '["Sales","Customer","Region","Product Details"]'
    assert sales.database.compatibilityLevel == 1600
    assert sales.cultures == []


def test_unknown_constructs_are_kept_not_dropped(sales):
    assert sales.model.extra_flags == {"discourageImplicitMeasures"}
    assert sales.table("Customer").column("Name").extra_properties == {"isAvailableInMdx": "false"}


def test_measures_inline_and_multiline(sales):
    revenue, margin = sales.table("Sales").measures
    assert (revenue.name, revenue.expression) == ("Revenue", "SUM(Sales[Amount])")
    assert revenue.description == "Total revenue in EUR."
    assert (revenue.formatString, revenue.displayFolder) == ("#,0.00", "KPIs")
    assert margin.name == "Margin %"
    assert margin.expression == "VAR revenue = [Revenue]\n\nRETURN\n\tDIVIDE(revenue - SUM(Sales[Cost]), revenue)"
    assert margin.formatString == "0.00%"


def test_columns(sales):
    table = sales.table("Sales")
    profit = table.column("Gross Profit")
    assert profit.is_calculated and profit.expression == "[Amount] - [Cost]"
    assert profit.description == "Amount minus cost, per order line."
    assert profit.sourceColumn is None

    order_id = table.column("OrderID")
    assert not order_id.is_calculated
    assert order_id.isKey and not order_id.isHidden and order_id.isNullable
    assert table.column("CustomerKey").isHidden

    [variation] = table.column("OrderDate").variations
    assert variation.isDefault
    assert variation.relationship == "6f0c2a51-0001-4000-8000-000000000904"
    assert variation.defaultHierarchy == "LocalDateTable_0001.'Date Hierarchy'"


def test_hierarchy_levels(sales):
    [hierarchy] = sales.table("Product Details").hierarchies
    assert hierarchy.name == "Product Hierarchy"
    assert [(level.name, level.column) for level in hierarchy.levels] == [
        ("Category", "Category"),
        ("Subcategory", "Subcategory"),
    ]


def test_partitions(sales):
    [partition] = sales.table("Sales").partitions
    assert (partition.sourceType, partition.mode) == ("m", "import")
    assert partition.source.startswith('let\n\tSource = Sql.Database("sql.example.com", "SalesDW"),')

    [calculated] = sales.table("LocalDateTable_0001").partitions
    assert calculated.sourceType == "calculated"
    assert calculated.source.startswith("Calendar(")


def test_relationships(sales):
    by_name = {relationship.name: relationship for relationship in sales.relationships}
    assert len(by_name) == 5

    flipped = by_name["6f0c2a51-0001-4000-8000-000000000903"]
    assert (flipped.fromCardinality, flipped.toCardinality) == ("one", "many")
    assert flipped.description == "Written one-to-many, so the mapping has to flip it."

    assert not by_name["6f0c2a51-0001-4000-8000-000000000905"].isActive
    assert by_name["AutoDetected_6f0c2a51-0001-4000-8000-000000000902"].is_auto_detected
    assert by_name["6f0c2a51-0001-4000-8000-000000000904"].joinOnDateBehavior == "datePartOnly"


def test_auto_date_tables(sales):
    date_table = sales.table("LocalDateTable_0001")
    assert date_table.is_auto_date_table
    assert date_table.isHidden and date_table.showAsVariationsOnly
    assert [table.name for table in sales.user_tables] == ["Sales", "Customer", "Region", "Product Details"]


def test_byte_order_mark_is_ignored(tmp_path):
    path = tmp_path / "t.tmdl"
    path.write_bytes("﻿table Ümsätze\n\tlineageTag: x\n".encode())
    table = parse_table_file(path)
    assert (table.name, table.lineageTag) == ("Ümsätze", "x")


# --------------------------------------------------------------------------- #
# The Retail sample, exported from Power BI Desktop
# --------------------------------------------------------------------------- #


def _objects(model):
    """Every object that carries the parser's leftover buckets, except cultures."""
    yield model.model
    yield from model.relationships
    for table in model.tables:
        yield table
        yield from table.measures
        yield from table.partitions
        for column in table.columns:
            yield column
            yield from column.variations
        for hierarchy in table.hierarchies:
            yield hierarchy
            yield from hierarchy.levels


def test_retail_sample_is_fully_understood():
    """The lossless invariant in reverse: a real export leaves nothing unrecognized.

    A new Power BI version that writes a construct the parser does not model
    will land in these buckets and fail here first.
    """
    model = parse_semantic_model(RETAIL)
    leftovers = {
        f"{type(obj).__name__} {obj.name}": (obj.extra_properties, obj.extra_flags, obj.extra_blocks)
        for obj in _objects(model)
        if obj.extra_properties or obj.extra_flags or obj.extra_blocks
    }
    assert leftovers == {}
    assert not model.database.extra_properties and not model.database.extra_flags
    assert [table.name for table in model.user_tables] == ["kunden", "produkte", "verkauefe"]
