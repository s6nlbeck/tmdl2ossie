"""Map a parsed TMDL semantic model onto an Apache Ossie document.

The rules live in ``docs/tmdl-to-ossie-mapping.md``; this module implements them
and nothing else. There is no intermediate representation -- the parser's
dataclasses go straight into the Ossie pydantic models.

Two shape differences drive the whole module. TMDL is table-centric: a table owns
its columns, its measures and the Power Query code that loads it. Ossie is
model-centric: datasets own fields, while metrics and relationships sit at the
root. So measures get lifted out of their table, and anything Ossie has no field
for -- hierarchies, formatting, visibility, lineage tags -- moves into
``custom_extensions`` rather than being dropped.

Conversion never raises on questionable input if it can help it: constructs that
cannot be expressed are skipped and reported in :attr:`ConversionResult.warnings`,
so one odd relationship does not cost the whole model. Validate the result with
``pbi_semantik.validation.validate_model`` before writing it out.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ossie.models import (
    OssieDataset,
    OssieDialect,
    OssieDialectExpression,
    OssieDimension,
    OssieDocument,
    OssieExpression,
    OssieField,
    OssieMetric,
    OssieRelationship,
)

from pbi_semantik.mapping_helpers import (
    TEMPORAL_TYPES,
    deduplicate,
    extras,
    non_default,
    pack_extension,
    resolve_datatype,
    resolve_source,
)
from tmdl_parser.datamodels import (
    TMDLColumn,
    TMDLMeasure,
    TMDLRelationship,
    TMDLSemanticModel,
    TMDLSummarizeBy,
    TMDLTable,
)
from tmdl_parser.helpers import split_column_reference


class MappingError(ValueError):
    """Raised when a model cannot be expressed as an Ossie document at all."""


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """The converted document plus everything that could not be carried over."""

    document: OssieDocument
    warnings: list[str]

    def report(self) -> str:
        return "\n".join(f"warning: {message}" for message in self.warnings) or "no warnings"


def convert(model: TMDLSemanticModel, *, name: str | None = None) -> ConversionResult:
    """Convert a parsed TMDL model into an Ossie document.

    *name* overrides the document name, which otherwise comes from the
    ``.SemanticModel`` directory. Power BI's auto-generated date tables and the
    relationships into them are filtered out.
    """
    return _Converter(model, name=name).run()


class _Converter:
    """One conversion run. Holds the warning log and the cross-object state."""

    def __init__(self, model: TMDLSemanticModel, *, name: str | None):
        self.model = model
        self.name = name
        self.warnings: list[str] = []
        # Keyed by name so relationship endpoints can be resolved and checked.
        self.tables: dict[str, TMDLTable] = {table.name: table for table in model.user_tables}
        self.auto_tables = {table.name for table in model.tables if table.is_auto_date_table}
        # Unique keys discovered while mapping relationships, per table name.
        self.unique_keys: dict[str, list[list[str]]] = {}

    def run(self) -> ConversionResult:
        if not self.tables:
            raise MappingError("model has no user tables; an Ossie document needs at least one dataset")

        # Relationships come first: they are what reveals which columns are
        # unique, and a dataset cannot express cardinality without that.
        relationships = self._relationships()
        datasets = [self._dataset(table) for table in self.tables.values()]
        metrics = self._metrics()

        document = OssieDocument(
            name=self._document_name(),
            description=self.model.model.description,
            datasets=datasets,
            # The models would serialize an empty list; the schema prefers absence.
            relationships=relationships or None,
            metrics=metrics or None,
            custom_extensions=self._document_extensions(),
        )
        return ConversionResult(document=document, warnings=self.warnings)

    def _warn(self, message: str) -> None:
        self.warnings.append(message)

    # ----------------------------------------------------------------------- #
    # Document
    # ----------------------------------------------------------------------- #

    def _document_name(self) -> str:
        if self.name:
            return self.name
        if self.model.source_path is not None:
            # source_path is the `definition` directory; its parent is named
            # `<model>.SemanticModel`. TMDL's own header is the constant
            # `model Model` and therefore useless as an identifier.
            return Path(self.model.source_path).parent.stem
        return self.model.model.name

    def _document_extensions(self):
        model = self.model.model
        database = self.model.database
        return pack_extension(
            culture=model.culture,
            defaultPowerBIDataSourceVersion=model.defaultPowerBIDataSourceVersion,
            sourceQueryCulture=model.sourceQueryCulture,
            valueFilterBehavior=model.valueFilterBehavior,
            dataAccessOptions=model.dataAccessOptions,
            compatibilityLevel=database.compatibilityLevel if database else None,
            annotations=model.annotations,
            **extras(model),
        )

    # ----------------------------------------------------------------------- #
    # Relationships
    # ----------------------------------------------------------------------- #

    def _relationships(self) -> list[OssieRelationship]:
        taken: set[str] = set()
        mapped = (self._relationship(item, taken) for item in self.model.relationships)
        return [item for item in mapped if item is not None]

    def _relationship(self, relationship: TMDLRelationship, taken: set[str]) -> OssieRelationship | None:
        label = f"relationship {relationship.name}"

        if not relationship.isActive:
            # Inactive relationships only ever apply inside a DAX USERELATIONSHIP,
            # which Ossie cannot express.
            self._warn(f"{label}: inactive, dropped")
            return None
        if not relationship.fromColumn or not relationship.toColumn:
            self._warn(f"{label}: missing endpoint, dropped")
            return None

        from_table, from_column = split_column_reference(relationship.fromColumn)
        to_table, to_column = split_column_reference(relationship.toColumn)

        # Filter on the endpoints, not on the `AutoDetected_` name prefix -- that
        # prefix only means Power BI inferred the relationship rather than the
        # author drawing it, and the genuine joins usually carry it too.
        if from_table in self.auto_tables or to_table in self.auto_tables:
            return None
        for table in (from_table, to_table):
            if table not in self.tables:
                self._warn(f"{label}: unknown table {table!r}, dropped")
                return None
        if not self._has_column(from_table, from_column) or not self._has_column(to_table, to_column):
            # Emitting it anyway would produce a dangling reference that the
            # upstream validator rejects.
            self._warn(f"{label}: references a column that does not exist, dropped")
            return None

        # Power BI writes the many side as `fromColumn` and normally omits the
        # cardinalities entirely; only an explicit one-to-many needs flipping.
        if relationship.fromCardinality == "one" and relationship.toCardinality == "many":
            from_table, from_column, to_table, to_column = to_table, to_column, from_table, from_column

        # Ossie reads cardinality off the target's keys, so Power BI's guarantee
        # that the "to" side is unique has to be moved onto that dataset.
        self._remember_unique_key(to_table, to_column)

        return OssieRelationship(
            name=self._relationship_name(from_table, to_table, from_column, taken),
            from_dataset=from_table,
            to=to_table,
            # TMDL relationships are always single-column: Power BI models a
            # composite key as a concatenated helper column instead.
            from_columns=[from_column],
            to_columns=[to_column],
            custom_extensions=pack_extension(
                # DAX measures may reference the relationship by its GUID.
                lineageTag=relationship.name,
                fromCardinality=relationship.fromCardinality,
                toCardinality=relationship.toCardinality,
                crossFilteringBehavior=relationship.crossFilteringBehavior,
                securityFilteringBehavior=relationship.securityFilteringBehavior,
                joinOnDateBehavior=relationship.joinOnDateBehavior,
                relyOnReferentialIntegrity=non_default(relationship.relyOnReferentialIntegrity, False),
                annotations=relationship.annotations,
                **extras(relationship),
            ),
        )

    def _relationship_name(self, from_table: str, to_table: str, from_column: str, taken: set[str]) -> str:
        """Readable replacement for TMDL's GUID, unique within the document."""
        name = f"{from_table}_to_{to_table}"
        if name in taken:
            # Two relationships between the same pair of tables: the foreign key
            # is what tells them apart.
            name = f"{name}_{from_column}"
        return deduplicate(name, taken)

    def _has_column(self, table: str, column: str) -> bool:
        return self.tables[table].column(column) is not None

    def _remember_unique_key(self, table: str, column: str) -> None:
        keys = self.unique_keys.setdefault(table, [])
        if [column] not in keys:
            keys.append([column])

    # ----------------------------------------------------------------------- #
    # Datasets and fields
    # ----------------------------------------------------------------------- #

    def _dataset(self, table: TMDLTable) -> OssieDataset:
        primary_key = [column.name for column in table.columns if column.isKey]
        # A key repeated as a unique key says nothing extra.
        unique_keys = [key for key in self.unique_keys.get(table.name, []) if key != primary_key]

        return OssieDataset(
            name=table.name,
            source=self._source(table),
            primary_key=primary_key or None,
            unique_keys=unique_keys or None,
            description=table.description,
            fields=[self._field(table, column) for column in table.columns] or None,
            custom_extensions=pack_extension(
                lineageTag=table.lineageTag,
                isHidden=non_default(table.isHidden, False),
                isPrivate=non_default(table.isPrivate, False),
                showAsVariationsOnly=non_default(table.showAsVariationsOnly, False),
                excludeFromModelRefresh=non_default(table.excludeFromModelRefresh, False),
                # Ossie has no hierarchies and no partitioning at all.
                hierarchies=table.hierarchies,
                partitions=table.partitions,
                annotations=table.annotations,
                **extras(table),
            ),
        )

    def _source(self, table: TMDLTable) -> str:
        """Resolve the dataset source, falling back to the table name.

        ``source`` is required, so there is no option of leaving it out when the
        partition turns out to be a local CSV. A bare table name is a valid
        address form, and the full M code survives in the dataset's extensions.
        """
        if not table.partitions:
            self._warn(f"table {table.name!r}: no partition, using the table name as source")
            return table.name
        if len(table.partitions) > 1:
            self._warn(
                f"table {table.name!r}: {len(table.partitions)} partitions, "
                "Ossie has no partitioning -- only the first defines the source"
            )

        partition = table.partitions[0]
        resolved = resolve_source(partition.sourceType, partition.source)
        if resolved:
            return resolved
        if partition.sourceType != "m":
            self._warn(f"table {table.name!r}: {partition.sourceType!r} partition, using the table name as source")
        return table.name

    def _field(self, table: TMDLTable, column: TMDLColumn) -> OssieField:
        datatype = resolve_datatype(column.dataType, column.annotations)
        return OssieField(
            name=column.name,
            expression=self._field_expression(table, column),
            dimension=self._dimension(column, datatype),
            # Loose fit -- the spec's own example value is "filter" -- but it is
            # the only slot Ossie offers for a grouping label.
            label=column.displayFolder,
            description=column.description,
            datatype=datatype,
            custom_extensions=pack_extension(
                lineageTag=column.lineageTag,
                dataType=column.dataType if datatype is None else None,
                formatString=column.formatString,
                summarizeBy=column.summarizeBy,
                dataCategory=column.dataCategory,
                sortByColumn=column.sortByColumn,
                displayFolder=column.displayFolder,
                isHidden=non_default(column.isHidden, False),
                isKey=non_default(column.isKey, False),
                isUnique=non_default(column.isUnique, False),
                isNullable=non_default(column.isNullable, True),
                isNameInferred=non_default(column.isNameInferred, False),
                isDataTypeInferred=non_default(column.isDataTypeInferred, False),
                showAsVariationsOnly=non_default(column.showAsVariationsOnly, False),
                variations=column.variations,
                annotations=column.annotations,
                **extras(column),
            ),
        )

    def _field_expression(self, table: TMDLTable, column: TMDLColumn) -> OssieExpression:
        if column.is_calculated:
            # DAX has a dialect of its own but no validation: sqlglot cannot parse
            # it, so the upstream validator skips the dialect entirely.
            return _expression(OssieDialect.DAX, column.expression)
        if column.sourceColumn:
            # A bare column reference is valid in every SQL dialect, which is why
            # the official examples use exactly this form.
            return _expression(OssieDialect.ANSI_SQL, column.sourceColumn)
        self._warn(
            f"column {table.name}.{column.name}: neither sourceColumn nor expression, "
            "using the column name as expression"
        )
        return _expression(OssieDialect.ANSI_SQL, column.name)

    def _dimension(self, column: TMDLColumn, datatype) -> OssieDimension | None:
        """Decide whether a column is a grouping key, and whether it is temporal."""
        if datatype in TEMPORAL_TYPES:
            # is_time already defaults to true for temporal types; repeating it
            # would only add noise.
            return OssieDimension()
        if column.summarizeBy == TMDLSummarizeBy.NONE:
            # "Do not summarize" is Power BI's closest signal for "this is an
            # attribute, not a number to aggregate" -- including foreign keys,
            # which is what the official TPC-DS example marks this way too.
            return OssieDimension(is_time=False)
        # Aggregable data: metric input rather than something to group by.
        return None

    # ----------------------------------------------------------------------- #
    # Metrics
    # ----------------------------------------------------------------------- #

    def _metrics(self) -> list[OssieMetric]:
        taken: set[str] = set()
        metrics = []
        for table in self.tables.values():
            for measure in table.measures:
                metrics.append(self._metric(table, measure, taken))
        return metrics

    def _metric(self, table: TMDLTable, measure: TMDLMeasure, taken: set[str]) -> OssieMetric:
        # Ossie metrics are model-level, so the owning table is flattened away.
        # Power BI keeps measure names unique across the model, so this should
        # not collide -- but a collision would silently shadow a metric.
        name = deduplicate(measure.name, taken)
        if name != measure.name:
            self._warn(f"measure {table.name}.{measure.name}: name already taken, renamed to {name!r}")
        if not measure.expression:
            self._warn(f"measure {table.name}.{measure.name}: no expression")

        return OssieMetric(
            name=name,
            # Verbatim DAX, line breaks and all. Never syntax-checked.
            expression=_expression(OssieDialect.DAX, measure.expression or measure.name),
            description=measure.description,
            datatype=resolve_datatype(measure.dataType, measure.annotations),
            custom_extensions=pack_extension(
                # The home table is not expressible anywhere else.
                table=table.name,
                lineageTag=measure.lineageTag,
                formatString=measure.formatString,
                displayFolder=measure.displayFolder,
                isHidden=non_default(measure.isHidden, False),
                annotations=measure.annotations,
                **extras(measure),
            ),
        )


def _expression(dialect: OssieDialect, expression: str) -> OssieExpression:
    return OssieExpression(dialects=[OssieDialectExpression(dialect=dialect, expression=expression)])
