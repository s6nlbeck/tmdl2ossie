"""Tests for the TMDL -> Ossie mapping, against the rules in ``docs/tmdl-to-ossie-mapping.md``.

The synthetic ``tests/fixtures/Sales.SemanticModel`` exercises the paths the
Retail sample cannot: measures, a calculated column, ``isKey``, SQL sources,
hierarchies and relationships that need flipping or dropping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ossie.models import OssieDataType, OssieDialect, OssieDimension

from tmdl2ossie.mapping import MappingError, convert
from tmdl2ossie.mapping_helpers import deduplicate, pack_extension, resolve_datatype, resolve_source
from tmdl2ossie.validation import validate_model
from tmdl_parser import parse_semantic_model
from tmdl_parser.datamodels import TMDLMeasure, TMDLPartition, TMDLRelationship

ROOT = Path(__file__).resolve().parents[1]
SALES = ROOT / "tests" / "fixtures" / "Sales.SemanticModel"
RETAIL = ROOT / "samples" / "Retail" / "pbi_file.SemanticModel"

INACTIVE_WARNING = "relationship 6f0c2a51-0001-4000-8000-000000000905: inactive, dropped"


@pytest.fixture
def model():
    """A fresh parse per test, so tests may modify it."""
    return parse_semantic_model(SALES)


@pytest.fixture(scope="module")
def result():
    return convert(parse_semantic_model(SALES))


@pytest.fixture(scope="module")
def doc(result):
    return result.document


def _dataset(doc, name):
    return next(dataset for dataset in doc.datasets if dataset.name == name)


def _field(doc, dataset, name):
    return next(field for field in _dataset(doc, dataset).fields if field.name == name)


def _extension(obj) -> dict:
    [extension] = obj.custom_extensions
    assert extension.vendor_name == "POWER_BI"
    return json.loads(extension.data)


def _only_expression(obj):
    [dialect] = obj.expression.dialects
    return dialect.dialect, dialect.expression


# --------------------------------------------------------------------------- #
# Whole document
# --------------------------------------------------------------------------- #


def test_result_is_valid_ossie(result):
    validation = validate_model(result.document)
    assert validation.ok, validation.report()
    assert validation.warnings == []


def test_only_the_inactive_relationship_is_reported(result):
    # The auto date relationship is dropped silently: it is Power BI plumbing,
    # not something the author needs to hear about.
    assert result.warnings == [INACTIVE_WARNING]


def test_document_root(doc):
    assert doc.name == "Sales"
    assert doc.description == "Order-line sales with customer, region and product dimensions."
    extension = _extension(doc)
    assert extension["culture"] == "en-US"
    assert extension["compatibilityLevel"] == 1600
    assert extension["extra_flags"] == ["discourageImplicitMeasures"]


def test_name_override(model):
    assert convert(model, name="Custom").document.name == "Custom"


def test_conversion_is_deterministic():
    first = convert(parse_semantic_model(SALES)).document.to_ossie_json()
    second = convert(parse_semantic_model(SALES)).document.to_ossie_json()
    assert first == second


# --------------------------------------------------------------------------- #
# Datasets
# --------------------------------------------------------------------------- #


def test_auto_date_tables_are_not_datasets(doc):
    assert [dataset.name for dataset in doc.datasets] == ["Sales", "Customer", "Region", "Product Details"]


@pytest.mark.parametrize(
    ("dataset", "source"),
    [
        # Sql.Database + navigation step -> database.schema.table
        ("Sales", "SalesDW.dbo.FactSales"),
        # Value.NativeQuery -> the query, with M's doubled quotes undone
        ("Customer", 'SELECT CustomerKey, "Full Name" AS Name, RegionKey FROM dbo.DimCustomer'),
        # Sql.Database(..., [Query=...]) -> the query
        ("Product Details", "SELECT ProductKey, Category, Subcategory, ListPrice FROM dbo.DimProduct"),
        # A local CSV has no portable address -> the table name
        ("Region", "Region"),
    ],
)
def test_dataset_source(doc, dataset, source):
    assert _dataset(doc, dataset).source == source


def test_full_m_code_survives_in_the_extension(doc):
    [partition] = _extension(_dataset(doc, "Region"))["partitions"]
    assert partition["sourceType"] == "m"
    assert 'Csv.Document(File.Contents("C:\\Data\\region.csv")' in partition["source"]


def test_keys(doc):
    names = ("Sales", "Customer", "Region", "Product Details")
    sales, customer, region, product = (_dataset(doc, name) for name in names)
    assert sales.primary_key == ["OrderID"]
    # Relationship target already the primary key -> not repeated as unique key.
    assert (customer.primary_key, customer.unique_keys) == (["CustomerKey"], None)
    # Relationship targets without isKey -> unique keys derived from the relationship.
    assert (region.primary_key, region.unique_keys) == (None, [["RegionKey"]])
    assert (product.primary_key, product.unique_keys) == (None, [["ProductKey"]])


def test_dataset_extension(doc):
    sales = _dataset(doc, "Sales")
    assert sales.description == "One row per order line."
    assert _extension(sales)["annotations"] == {"PBI_ResultType": "Table"}
    [hierarchy] = _extension(_dataset(doc, "Product Details"))["hierarchies"]
    assert [level["column"] for level in hierarchy["levels"]] == ["Category", "Subcategory"]


# --------------------------------------------------------------------------- #
# Fields
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("dataset", "field", "datatype"),
    [
        ("Sales", "OrderID", OssieDataType.INTEGER),
        ("Sales", "OrderDate", OssieDataType.DATE),  # dateTime + UnderlyingDateTimeDataType
        ("Sales", "ProductKey", OssieDataType.STRING),
        ("Sales", "Amount", OssieDataType.DECIMAL),  # Power BI "fixed decimal"
        ("Sales", "Cost", OssieDataType.FLOAT),  # Power BI "decimal number"
    ],
)
def test_field_datatype(doc, dataset, field, datatype):
    assert _field(doc, dataset, field).datatype == datatype


@pytest.mark.parametrize(
    ("field", "dimension"),
    [
        ("OrderDate", OssieDimension()),  # temporal: is_time left to its default
        ("CustomerKey", OssieDimension(is_time=False)),  # summarizeBy: none
        ("Amount", None),  # summarizeBy: sum -> metric input, not a dimension
    ],
)
def test_field_dimension(doc, field, dimension):
    assert _field(doc, "Sales", field).dimension == dimension


def test_data_and_calculated_columns(doc):
    assert _only_expression(_field(doc, "Sales", "Amount")) == (OssieDialect.ANSI_SQL, "Amount")
    profit = _field(doc, "Sales", "Gross Profit")
    assert _only_expression(profit) == (OssieDialect.DAX, "[Amount] - [Cost]")
    assert profit.label == "Profitability"
    assert profit.description == "Amount minus cost, per order line."


def test_field_extension_keeps_only_what_was_set(doc):
    extension = _extension(_field(doc, "Sales", "CustomerKey"))
    assert extension["isHidden"] is True
    # Defaults (isNullable: true, isKey: false, ...) are not repeated.
    assert "isNullable" not in extension and "isKey" not in extension
    # Unknown TMDL properties still make it into the output.
    assert _extension(_field(doc, "Customer", "Name"))["extra_properties"] == {"isAvailableInMdx": "false"}


# --------------------------------------------------------------------------- #
# Relationships
# --------------------------------------------------------------------------- #


def test_relationships(doc):
    relationships = {r.name: (r.from_dataset, r.to, r.from_columns, r.to_columns) for r in doc.relationships}
    assert relationships == {
        "Sales_to_Customer": ("Sales", "Customer", ["CustomerKey"], ["CustomerKey"]),
        # AutoDetected_ prefix, but both ends are user tables -> kept.
        "Sales_to_Product Details": ("Sales", "Product Details", ["ProductKey"], ["ProductKey"]),
        # Written as one-to-many in TMDL -> flipped so `from` is the many side.
        "Customer_to_Region": ("Customer", "Region", ["RegionKey"], ["RegionKey"]),
    }


def test_relationship_keeps_its_guid(doc):
    relationship = next(r for r in doc.relationships if r.name == "Customer_to_Region")
    assert _extension(relationship) == {
        "lineageTag": "6f0c2a51-0001-4000-8000-000000000903",
        "fromCardinality": "one",
        "toCardinality": "many",
    }


def test_second_relationship_between_the_same_tables_gets_its_column_in_the_name(model):
    inactive = next(r for r in model.relationships if not r.isActive)
    inactive.isActive = True
    # Power BI forbids two active paths, so in TMDL this one stays inactive --
    # but the naming rule must still produce distinct names.
    names = [r.name for r in convert(model).document.relationships]
    assert "Sales_to_Customer" in names and "Sales_to_Customer_BillToCustomerKey" in names


@pytest.mark.parametrize(
    ("from_column", "to_column", "warning"),
    [
        ("Sales.Missing", "Customer.CustomerKey", "references a column that does not exist, dropped"),
        ("Nowhere.Key", "Customer.CustomerKey", "unknown table 'Nowhere', dropped"),
        (None, "Customer.CustomerKey", "missing endpoint, dropped"),
    ],
)
def test_broken_relationships_are_dropped_with_a_warning(model, from_column, to_column, warning):
    model.relationships.append(TMDLRelationship(name="broken", fromColumn=from_column, toColumn=to_column))
    result = convert(model)
    assert f"relationship broken: {warning}" in result.warnings
    assert len(result.document.relationships) == 3
    assert validate_model(result.document).ok


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_metrics(doc):
    revenue, margin = doc.metrics
    assert (revenue.name, _only_expression(revenue)) == ("Revenue", (OssieDialect.DAX, "SUM(Sales[Amount])"))
    assert revenue.description == "Total revenue in EUR."
    assert _extension(revenue) == {
        "table": "Sales",
        "lineageTag": "6f0c2a51-0001-4000-8000-000000000101",
        "formatString": "#,0.00",
        "displayFolder": "KPIs",
    }
    # Multi-line DAX stays verbatim, blank line and indentation included.
    assert _only_expression(margin) == (
        OssieDialect.DAX,
        "VAR revenue = [Revenue]\n\nRETURN\n\tDIVIDE(revenue - SUM(Sales[Cost]), revenue)",
    )


def test_measure_name_collision_is_renamed(model):
    model.table("Customer").measures.append(TMDLMeasure(name="Revenue", expression="COUNTROWS(Customer)"))
    result = convert(model)
    assert [metric.name for metric in result.document.metrics] == ["Revenue", "Margin %", "Revenue_2"]
    assert "measure Customer.Revenue: name already taken, renamed to 'Revenue_2'" in result.warnings


# --------------------------------------------------------------------------- #
# Awkward tables
# --------------------------------------------------------------------------- #


def test_table_without_partition(model):
    model.table("Region").partitions.clear()
    result = convert(model)
    assert _dataset(result.document, "Region").source == "Region"
    assert "table 'Region': no partition, using the table name as source" in result.warnings


def test_table_with_several_partitions(model):
    sales = model.table("Sales")
    sales.partitions.append(TMDLPartition(name="Sales 2025", sourceType="m", source="let x = 1 in x"))
    result = convert(model)
    # The first partition still decides the source; all of them are kept.
    assert _dataset(result.document, "Sales").source == "SalesDW.dbo.FactSales"
    assert len(_extension(_dataset(result.document, "Sales"))["partitions"]) == 2
    assert any("2 partitions" in warning for warning in result.warnings)


def test_model_with_only_auto_date_tables(model):
    model.tables = [table for table in model.tables if table.is_auto_date_table]
    with pytest.raises(MappingError):
        convert(model)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("data_type", "annotations", "expected"),
    [
        ("int64", {}, OssieDataType.INTEGER),
        ("boolean", {}, OssieDataType.BOOLEAN),
        ("dateTime", {}, OssieDataType.DATE_TIME),
        ("dateTime", {"UnderlyingDateTimeDataType": "Time"}, OssieDataType.TIME),
        ("binary", {}, OssieDataType.OPAQUE),
        ("automatic", {}, None),
        (None, {}, None),
    ],
)
def test_resolve_datatype(data_type, annotations, expected):
    assert resolve_datatype(data_type, annotations) == expected


@pytest.mark.parametrize(
    ("source_type", "code", "expected"),
    [
        ("m", 'Source = Sql.Database("s", "db"), t = Source{[Schema="sales",Item="Orders"]}[Data]', "db.sales.Orders"),
        ("m", 'Value.NativeQuery(Source, "SELECT 1")', "SELECT 1"),
        ("m", 'Csv.Document(File.Contents("C:\\x.csv"))', None),
        ("calculated", 'Value.NativeQuery(Source, "SELECT 1")', None),
        ("m", None, None),
    ],
)
def test_resolve_source(source_type, code, expected):
    assert resolve_source(source_type, code) == expected


def test_pack_extension_drops_empty_values():
    assert pack_extension(a=None, b=False, c=[], d={}) is None
    [extension] = pack_extension(a="x", b=0, c={"y"}, d=None)
    # 0 is information, unlike None/False/empty; sets become sorted lists.
    assert json.loads(extension.data) == {"a": "x", "b": 0, "c": ["y"]}


def test_deduplicate():
    taken: set[str] = set()
    assert [deduplicate("a", taken) for _ in range(3)] == ["a", "a_2", "a_3"]


# --------------------------------------------------------------------------- #
# The Retail sample, exported from Power BI Desktop
# --------------------------------------------------------------------------- #


def test_retail_sample_converts_cleanly():
    result = convert(parse_semantic_model(RETAIL))
    assert result.warnings == []
    assert validate_model(result.document).ok
    assert [r.name for r in result.document.relationships] == ["verkauefe_to_kunden", "verkauefe_to_produkte"]
    assert result.document.metrics is None
