"""Read a Power BI TMDL semantic model from disk into :mod:`tmdl_parser.datamodels` objects.

``helpers.parse_document`` does the grammar work and hands back a tree of
:class:`~tmdl_parser.helpers.TMDLBlock` nodes; this module only interprets that
tree -- which keyword means which dataclass, which property maps to which field.

Everything the mapping does not recognize is kept: unknown properties go to
``extra_properties``, unknown flags to ``extra_flags``, unknown nested blocks to
``extra_blocks``. A TMDL construct we have never seen therefore survives a parse
instead of disappearing silently.
"""

from __future__ import annotations

from pathlib import Path

from tmdl_parser.datamodels import (
    TMDLColumn,
    TMDLCulture,
    TMDLDatabase,
    TMDLHierarchy,
    TMDLHierarchyLevel,
    TMDLMeasure,
    TMDLModel,
    TMDLPartition,
    TMDLRelationship,
    TMDLSemanticModel,
    TMDLTable,
    TMDLVariation,
)
from tmdl_parser.helpers import TMDLBlock, TMDLParseError, parse_bool, parse_document

MODEL_FILE = "model.tmdl"
DATABASE_FILE = "database.tmdl"
RELATIONSHIPS_FILE = "relationships.tmdl"
TABLES_DIR = "tables"
CULTURES_DIR = "cultures"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def parse_semantic_model(path: str | Path) -> TMDLSemanticModel:
    """Parse a complete semantic model.

    *path* may point at the ``definition`` directory, at the enclosing
    ``<name>.SemanticModel`` directory, or at a .pbip project root holding one.
    """
    definition = _resolve_definition_dir(Path(path))

    # model.tmdl is the entry point: it lists which tables belong to the model.
    model = parse_model_file(definition / MODEL_FILE)

    database_file = definition / DATABASE_FILE
    database = parse_database_file(database_file) if database_file.is_file() else None

    relationships_file = definition / RELATIONSHIPS_FILE
    relationships = parse_relationships_file(relationships_file) if relationships_file.is_file() else []

    # Read every table file rather than deriving file names from the `ref table`
    # entries -- Power BI escapes characters that are illegal in file names, so
    # the two do not always match. The refs are used for ordering only.
    tables = [parse_table_file(file) for file in sorted((definition / TABLES_DIR).glob("*.tmdl"))]
    tables = _order_by_refs(tables, model.tableRefs)

    cultures = [parse_culture_file(file) for file in sorted((definition / CULTURES_DIR).glob("*.tmdl"))]

    return TMDLSemanticModel(
        model=model,
        database=database,
        tables=tables,
        relationships=relationships,
        cultures=cultures,
        source_path=definition,
    )


def parse_model_file(path: str | Path) -> TMDLModel:
    """Parse ``model.tmdl``."""
    root = _read_document(path)
    block = root.first("model")
    if block is None:
        raise TMDLParseError("no model block found", path=str(path))

    properties, flags = _bag(block)
    # dataAccessOptions is a block whose body is nothing but bare flags.
    data_access = block.first("dataAccessOptions")
    model = TMDLModel(
        name=block.name or "",
        description=block.description,
        lineageTag=properties.pop("lineageTag", None),
        culture=properties.pop("culture", None),
        defaultPowerBIDataSourceVersion=properties.pop("defaultPowerBIDataSourceVersion", None),
        sourceQueryCulture=properties.pop("sourceQueryCulture", None),
        valueFilterBehavior=properties.pop("valueFilterBehavior", None),
        dataAccessOptions=list(data_access.flags) if data_access else [],
        # Annotations and `ref` lines are written at indent 0, as siblings of the
        # model block rather than inside it, so merge the root's entries back in.
        annotations={**block.annotations, **root.annotations},
        extra_properties=properties,
        extra_flags=flags,
        extra_blocks=_unknown_children(block, "dataAccessOptions", "ref"),
    )

    for ref in root.children_of("ref") + block.children_of("ref"):
        kind, _, name = (ref.name or "").partition(" ")
        if kind == "table":
            model.tableRefs.append(name.strip())
        elif kind == "cultureInfo":
            model.cultureRefs.append(name.strip())

    return model


def parse_database_file(path: str | Path) -> TMDLDatabase:
    """Parse ``database.tmdl``."""
    root = _read_document(path)
    block = root.first("database")
    if block is None:
        raise TMDLParseError("no database block found", path=str(path))

    properties, flags = _bag(block)
    return TMDLDatabase(
        compatibilityLevel=_pop_int(properties, "compatibilityLevel"),
        annotations=dict(block.annotations),
        extra_properties=properties,
        extra_flags=flags,
    )


def parse_relationships_file(path: str | Path) -> list[TMDLRelationship]:
    """Parse ``relationships.tmdl`` into one object per relationship block."""
    root = _read_document(path)
    return [_build_relationship(block) for block in root.children_of("relationship")]


def parse_table_file(path: str | Path) -> TMDLTable:
    """Parse a single ``tables/<name>.tmdl`` file."""
    root = _read_document(path)
    block = root.first("table")
    if block is None:
        raise TMDLParseError("no table block found", path=str(path))
    return _build_table(block)


def parse_culture_file(path: str | Path) -> TMDLCulture:
    """Parse a ``cultures/<locale>.tmdl`` file.

    Only the header is interpreted. The linguistic metadata below it is a large
    JSON document that no later pack needs, so it is kept as raw text.
    """
    text = _read_text(path)
    root = parse_document(text, path=str(path))
    block = root.first("cultureInfo")
    if block is None:
        raise TMDLParseError("no cultureInfo block found", path=str(path))

    properties, flags = _bag(block)
    return TMDLCulture(
        name=block.name or "",
        description=block.description,
        lineageTag=properties.pop("lineageTag", None),
        content=text,
        annotations=dict(block.annotations),
        extra_properties=properties,
        extra_flags=flags,
        extra_blocks=list(block.children),
    )


# --------------------------------------------------------------------------- #
# Block -> dataclass mapping
# --------------------------------------------------------------------------- #


def _build_table(block: TMDLBlock) -> TMDLTable:
    properties, flags = _bag(block)
    return TMDLTable(
        name=block.name or "",
        description=block.description,
        lineageTag=properties.pop("lineageTag", None),
        columns=[_build_column(child) for child in block.children_of("column")],
        measures=[_build_measure(child) for child in block.children_of("measure")],
        hierarchies=[_build_hierarchy(child) for child in block.children_of("hierarchy")],
        partitions=[_build_partition(child) for child in block.children_of("partition")],
        isHidden=_pop_bool(properties, flags, "isHidden"),
        isPrivate=_pop_bool(properties, flags, "isPrivate"),
        excludeFromModelRefresh=_pop_bool(properties, flags, "excludeFromModelRefresh"),
        showAsVariationsOnly=_pop_bool(properties, flags, "showAsVariationsOnly"),
        annotations=dict(block.annotations),
        # Listed last on purpose: by now every known key has been popped, so what
        # remains in these two containers is exactly the unrecognized rest.
        extra_properties=properties,
        extra_flags=flags,
        extra_blocks=_unknown_children(block, "column", "measure", "hierarchy", "partition"),
    )


def _build_column(block: TMDLBlock) -> TMDLColumn:
    properties, flags = _bag(block)
    return TMDLColumn(
        name=block.name or "",
        description=block.description,
        lineageTag=properties.pop("lineageTag", None),
        # Present only on calculated columns, written as `column Jahr = YEAR([Date])`.
        expression=block.expression,
        dataType=properties.pop("dataType", None),
        sourceColumn=properties.pop("sourceColumn", None),
        formatString=properties.pop("formatString", None),
        summarizeBy=properties.pop("summarizeBy", None),
        dataCategory=properties.pop("dataCategory", None),
        displayFolder=properties.pop("displayFolder", None),
        sortByColumn=properties.pop("sortByColumn", None),
        isHidden=_pop_bool(properties, flags, "isHidden"),
        isKey=_pop_bool(properties, flags, "isKey"),
        isNullable=_pop_bool(properties, flags, "isNullable", default=True),
        isUnique=_pop_bool(properties, flags, "isUnique"),
        isNameInferred=_pop_bool(properties, flags, "isNameInferred"),
        isDataTypeInferred=_pop_bool(properties, flags, "isDataTypeInferred"),
        showAsVariationsOnly=_pop_bool(properties, flags, "showAsVariationsOnly"),
        variations=[_build_variation(child) for child in block.children_of("variation")],
        annotations=dict(block.annotations),
        extra_properties=properties,
        extra_flags=flags,
        extra_blocks=_unknown_children(block, "variation"),
    )


def _build_variation(block: TMDLBlock) -> TMDLVariation:
    properties, flags = _bag(block)
    return TMDLVariation(
        name=block.name or "",
        description=block.description,
        lineageTag=properties.pop("lineageTag", None),
        isDefault=_pop_bool(properties, flags, "isDefault"),
        relationship=properties.pop("relationship", None),
        defaultHierarchy=properties.pop("defaultHierarchy", None),
        annotations=dict(block.annotations),
        extra_properties=properties,
        extra_flags=flags,
        extra_blocks=list(block.children),
    )


def _build_measure(block: TMDLBlock) -> TMDLMeasure:
    properties, flags = _bag(block)
    return TMDLMeasure(
        name=block.name or "",
        description=block.description,
        lineageTag=properties.pop("lineageTag", None),
        # The DAX follows the `=` of the header, inline or as an indented block.
        expression=block.expression,
        dataType=properties.pop("dataType", None),
        formatString=properties.pop("formatString", None),
        displayFolder=properties.pop("displayFolder", None),
        isHidden=_pop_bool(properties, flags, "isHidden"),
        annotations=dict(block.annotations),
        extra_properties=properties,
        extra_flags=flags,
        extra_blocks=list(block.children),
    )


def _build_hierarchy(block: TMDLBlock) -> TMDLHierarchy:
    properties, flags = _bag(block)
    return TMDLHierarchy(
        name=block.name or "",
        description=block.description,
        lineageTag=properties.pop("lineageTag", None),
        isHidden=_pop_bool(properties, flags, "isHidden"),
        displayFolder=properties.pop("displayFolder", None),
        # Levels are written in order; TMDL omits `ordinal` when it is implied.
        levels=[_build_level(child) for child in block.children_of("level")],
        annotations=dict(block.annotations),
        extra_properties=properties,
        extra_flags=flags,
        extra_blocks=_unknown_children(block, "level"),
    )


def _build_level(block: TMDLBlock) -> TMDLHierarchyLevel:
    properties, flags = _bag(block)
    return TMDLHierarchyLevel(
        name=block.name or "",
        description=block.description,
        lineageTag=properties.pop("lineageTag", None),
        ordinal=_pop_int(properties, "ordinal"),
        column=properties.pop("column", None),
        annotations=dict(block.annotations),
        extra_properties=properties,
        extra_flags=flags,
        extra_blocks=list(block.children),
    )


def _build_partition(block: TMDLBlock) -> TMDLPartition:
    properties, flags = _bag(block)
    return TMDLPartition(
        name=block.name or "",
        description=block.description,
        lineageTag=properties.pop("lineageTag", None),
        # For a partition the header assignment is the source *type*, not an
        # expression: `partition kunden = m` means the query below is M code.
        sourceType=block.expression,
        mode=properties.pop("mode", None),
        source=properties.pop("source", None),
        annotations=dict(block.annotations),
        extra_properties=properties,
        extra_flags=flags,
        extra_blocks=list(block.children),
    )


def _build_relationship(block: TMDLBlock) -> TMDLRelationship:
    properties, flags = _bag(block)
    return TMDLRelationship(
        # The header name is the relationship GUID, not a readable name.
        name=block.name or "",
        description=block.description,
        lineageTag=properties.pop("lineageTag", None),
        fromColumn=properties.pop("fromColumn", None),
        toColumn=properties.pop("toColumn", None),
        fromCardinality=properties.pop("fromCardinality", None),
        toCardinality=properties.pop("toCardinality", None),
        crossFilteringBehavior=properties.pop("crossFilteringBehavior", None),
        securityFilteringBehavior=properties.pop("securityFilteringBehavior", None),
        joinOnDateBehavior=properties.pop("joinOnDateBehavior", None),
        relyOnReferentialIntegrity=_pop_bool(properties, flags, "relyOnReferentialIntegrity"),
        # TMDL only writes `isActive` when a relationship is inactive.
        isActive=_pop_bool(properties, flags, "isActive", default=True),
        annotations=dict(block.annotations),
        extra_properties=properties,
        extra_flags=flags,
        extra_blocks=list(block.children),
    )


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _bag(block: TMDLBlock) -> tuple[dict[str, str], set[str]]:
    """Copy a block's properties and flags so the builders can pop them destructively."""
    return dict(block.properties), set(block.flags)


def _pop_bool(properties: dict[str, str], flags: set[str], key: str, *, default: bool = False) -> bool:
    """Read a TMDL boolean, which comes in two spellings.

    A true value is normally written as a bare flag line (``isHidden``), while an
    explicit value appears as a property (``isActive: false``). Accept both.
    """
    if key in flags:
        flags.discard(key)
        return True
    if key in properties:
        return parse_bool(properties.pop(key), default=default)
    return default


def _pop_int(properties: dict[str, str], key: str) -> int | None:
    value = properties.pop(key, None)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        # Keep the unparsable text instead of losing it.
        properties[key] = value
        return None


def _unknown_children(block: TMDLBlock, *known: str) -> list[TMDLBlock]:
    return [child for child in block.children if child.keyword not in known]


def _order_by_refs(tables: list[TMDLTable], refs: list[str]) -> list[TMDLTable]:
    """Put tables in the order ``model.tmdl`` lists them, keeping any extras at the end."""
    by_name = {table.name: table for table in tables}
    ordered = [by_name.pop(ref) for ref in refs if ref in by_name]
    return ordered + [table for table in tables if table.name in by_name]


def _resolve_definition_dir(path: Path) -> Path:
    """Find the directory holding ``model.tmdl``, accepting the usual entry points."""
    if path.is_file():
        path = path.parent
    if (path / MODEL_FILE).is_file():
        return path
    if (path / "definition" / MODEL_FILE).is_file():
        return path / "definition"
    # A .pbip project root contains the semantic model as a sibling folder.
    for candidate in sorted(path.glob("*.SemanticModel")):
        if (candidate / "definition" / MODEL_FILE).is_file():
            return candidate / "definition"
    raise TMDLParseError(f"no {MODEL_FILE} found under {path}", path=str(path))


def _read_text(path: str | Path) -> str:
    # TMDL files are UTF-8 and Power BI writes them with a BOM.
    return Path(path).read_text(encoding="utf-8-sig")


def _read_document(path: str | Path) -> TMDLBlock:
    return parse_document(_read_text(path), path=str(path))
