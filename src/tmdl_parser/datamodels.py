"""Dataclasses holding the components of a TMDL semantic model.

These types are a faithful, lossless mirror of the TMDL files on disk -- they do
not interpret or normalize anything. Opinionated mapping (dropping auto-generated
date tables, resolving column references, flattening to the application JSON
format) belongs in the later packs, not here.

Property names deliberately keep TMDL's camelCase spelling (``lineageTag``,
``sourceColumn``, ``summarizeBy``) so that a parsed object lines up 1:1 with the
text it came from. Every object carries ``annotations`` plus the ``extra_properties``
/ ``extra_flags`` escape hatches, so TMDL constructs this module does not model
explicitly survive a parse instead of being silently dropped.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tmdl_parser.helpers import TMDLBlock

# Auto-generated time intelligence tables Power BI maintains on its own. They are
# hidden+private and carry no user-authored semantics.
AUTO_DATE_TABLE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")
AUTO_DATE_TABLE_ANNOTATIONS = ("__PBI_LocalDateTable", "__PBI_TemplateDateTable")


class TMDLDataType(StrEnum):
    """Known column/measure data types. Values compare equal to the raw strings."""

    STRING = "string"
    INT64 = "int64"
    DOUBLE = "double"
    DECIMAL = "decimal"
    DATETIME = "dateTime"
    BOOLEAN = "boolean"
    BINARY = "binary"
    VARIANT = "variant"
    AUTOMATIC = "automatic"
    UNKNOWN = "unknown"


class TMDLSummarizeBy(StrEnum):
    NONE = "none"
    SUM = "sum"
    COUNT = "count"
    MIN = "min"
    MAX = "max"
    AVERAGE = "average"
    DISTINCT_COUNT = "distinctCount"


class TMDLPartitionSourceType(StrEnum):
    """The ``= <type>`` suffix of a partition header, e.g. ``partition kunden = m``."""

    M = "m"
    CALCULATED = "calculated"
    QUERY = "query"
    ENTITY = "entity"
    CALCULATION_GROUP = "calculationGroup"


@dataclass(slots=True, kw_only=True)
class TMDLObject:
    """Fields shared by every named TMDL object.

    ``description`` is the text of the ``/// ...`` comment lines preceding the
    object -- the business documentation that matters most downstream.
    """

    name: str
    description: str | None = None
    lineageTag: str | None = None
    annotations: dict[str, str] = field(default_factory=dict)
    # Properties (``key: value``), bare boolean flags and nested blocks this
    # module does not model explicitly, kept so nothing is lost on a round trip.
    extra_properties: dict[str, str] = field(default_factory=dict)
    extra_flags: set[str] = field(default_factory=set)
    extra_blocks: list["TMDLBlock"] = field(default_factory=list)


@dataclass(slots=True, kw_only=True)
class TMDLVariation(TMDLObject):
    """A column variation, linking a date column to its auto date hierarchy."""

    isDefault: bool = False
    relationship: str | None = None
    defaultHierarchy: str | None = None


@dataclass(slots=True, kw_only=True)
class TMDLColumn(TMDLObject):
    """A data, calculated, or calculated-table column.

    A calculated column carries its DAX in ``expression`` (written inline in TMDL
    as ``column Jahr = YEAR([Date])``); a plain data column instead has
    ``sourceColumn`` pointing at the partition's query output.
    """

    dataType: str | None = None
    expression: str | None = None
    sourceColumn: str | None = None
    formatString: str | None = None
    summarizeBy: str | None = None
    dataCategory: str | None = None
    displayFolder: str | None = None
    sortByColumn: str | None = None
    isHidden: bool = False
    isKey: bool = False
    isNullable: bool = True
    isUnique: bool = False
    isNameInferred: bool = False
    isDataTypeInferred: bool = False
    showAsVariationsOnly: bool = False
    variations: list[TMDLVariation] = field(default_factory=list)

    @property
    def is_calculated(self) -> bool:
        return self.expression is not None


@dataclass(slots=True, kw_only=True)
class TMDLMeasure(TMDLObject):
    """A DAX measure. ``expression`` holds the DAX, inline or as an indented block."""

    expression: str | None = None
    dataType: str | None = None
    formatString: str | None = None
    displayFolder: str | None = None
    isHidden: bool = False


@dataclass(slots=True, kw_only=True)
class TMDLHierarchyLevel(TMDLObject):
    """One level of a hierarchy, referencing a column of the owning table."""

    ordinal: int | None = None
    column: str | None = None


@dataclass(slots=True, kw_only=True)
class TMDLHierarchy(TMDLObject):
    isHidden: bool = False
    displayFolder: str | None = None
    levels: list[TMDLHierarchyLevel] = field(default_factory=list)


@dataclass(slots=True, kw_only=True)
class TMDLPartition(TMDLObject):
    """A table partition and the query that loads it.

    ``source`` is the raw expression text -- M for ``sourceType == "m"``, DAX for
    ``"calculated"`` -- kept verbatim, including its original line breaks.
    """

    sourceType: str | None = None
    mode: str | None = None
    source: str | None = None


@dataclass(slots=True, kw_only=True)
class TMDLTable(TMDLObject):
    columns: list[TMDLColumn] = field(default_factory=list)
    measures: list[TMDLMeasure] = field(default_factory=list)
    hierarchies: list[TMDLHierarchy] = field(default_factory=list)
    partitions: list[TMDLPartition] = field(default_factory=list)
    isHidden: bool = False
    isPrivate: bool = False
    excludeFromModelRefresh: bool = False
    # Set on the auto date tables: the table only surfaces through a column variation.
    showAsVariationsOnly: bool = False

    @property
    def is_auto_date_table(self) -> bool:
        """Whether Power BI generated this table for time intelligence."""
        return self.name.startswith(AUTO_DATE_TABLE_PREFIXES) or any(
            key in self.annotations for key in AUTO_DATE_TABLE_ANNOTATIONS
        )

    def column(self, name: str) -> TMDLColumn | None:
        return next((c for c in self.columns if c.name == name), None)

    def measure(self, name: str) -> TMDLMeasure | None:
        return next((m for m in self.measures if m.name == name), None)


@dataclass(slots=True, kw_only=True)
class TMDLRelationship(TMDLObject):
    """A relationship between two columns.

    ``name`` is the GUID from the block header. ``fromColumn`` / ``toColumn`` stay
    as the raw ``Table.Column`` text; splitting them (and handling quoted names)
    is the parsing helpers' job.
    """

    fromColumn: str | None = None
    toColumn: str | None = None
    fromCardinality: str | None = None
    toCardinality: str | None = None
    crossFilteringBehavior: str | None = None
    securityFilteringBehavior: str | None = None
    joinOnDateBehavior: str | None = None
    relyOnReferentialIntegrity: bool = False
    isActive: bool = True

    @property
    def is_auto_detected(self) -> bool:
        """Whether Power BI inferred this relationship rather than the author."""
        return self.name.startswith("AutoDetected_")


@dataclass(slots=True, kw_only=True)
class TMDLCulture(TMDLObject):
    """A ``cultureInfo`` block. Its linguistic metadata is kept as raw text."""

    content: str | None = None


@dataclass(slots=True, kw_only=True)
class TMDLDatabase:
    compatibilityLevel: int | None = None
    annotations: dict[str, str] = field(default_factory=dict)
    extra_properties: dict[str, str] = field(default_factory=dict)
    extra_flags: set[str] = field(default_factory=set)


@dataclass(slots=True, kw_only=True)
class TMDLModel(TMDLObject):
    """The root ``model.tmdl`` block.

    ``ref table`` / ``ref cultureInfo`` lines are kept in ``tableRefs`` /
    ``cultureRefs``; they are the inventory telling the parser which files to read.
    """

    culture: str | None = None
    defaultPowerBIDataSourceVersion: str | None = None
    sourceQueryCulture: str | None = None
    valueFilterBehavior: str | None = None
    dataAccessOptions: list[str] = field(default_factory=list)
    tableRefs: list[str] = field(default_factory=list)
    cultureRefs: list[str] = field(default_factory=list)


@dataclass(slots=True, kw_only=True)
class TMDLSemanticModel:
    """Everything parsed from one ``<name>.SemanticModel/definition`` directory."""

    model: TMDLModel
    database: TMDLDatabase | None = None
    tables: list[TMDLTable] = field(default_factory=list)
    relationships: list[TMDLRelationship] = field(default_factory=list)
    cultures: list[TMDLCulture] = field(default_factory=list)
    source_path: Path | None = None

    def table(self, name: str) -> TMDLTable | None:
        return next((t for t in self.tables if t.name == name), None)

    @property
    def user_tables(self) -> list[TMDLTable]:
        """Author-defined tables, without Power BI's auto date tables."""
        return [t for t in self.tables if not t.is_auto_date_table]
