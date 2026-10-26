"""Helpers for the TMDL -> Ossie mapping.

Everything here is a pure lookup or text transformation: the data type table, the
best-effort reading of a partition's M code, and the packing of leftover Power BI
metadata into Ossie's ``custom_extensions``. Deciding *which* TMDL object becomes
which Ossie object is the job of ``mapping.py``.

The rules implemented here are specified in ``docs/tmdl-to-ossie-mapping.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from typing import Any

from ossie.models import OssieCustomExtension, OssieDataType

# Free-form per the schema; `POWER_BI` is the spelling spec.yaml uses as example.
POWER_BI_VENDOR = "POWER_BI"

# TMDL dataType -> Ossie datatype. Note that Power BI's naming runs opposite to
# Ossie's: its "Decimal number" is binary floating point (`double` -> Float),
# while "Fixed decimal number" is the exact one (`decimal` -> Decimal).
DATA_TYPES: dict[str, OssieDataType] = {
    "string": OssieDataType.STRING,
    "int64": OssieDataType.INTEGER,
    "double": OssieDataType.FLOAT,
    "decimal": OssieDataType.DECIMAL,
    "boolean": OssieDataType.BOOLEAN,
    "dateTime": OssieDataType.DATE_TIME,
    # No portable type; the original is preserved in a custom extension.
    "binary": OssieDataType.OPAQUE,
    "variant": OssieDataType.OPAQUE,
    # `automatic` and `unknown` are deliberately absent: omitting the optional
    # `datatype` is more honest than guessing one.
}

# Power BI stores Date and Time columns as `dateTime` and records what the user
# actually picked in this annotation.
UNDERLYING_TYPE_ANNOTATION = "UnderlyingDateTimeDataType"
UNDERLYING_TYPES: dict[str, OssieDataType] = {
    "Date": OssieDataType.DATE,
    "Time": OssieDataType.TIME,
    "DateTime": OssieDataType.DATE_TIME,
}

TEMPORAL_TYPES = frozenset(
    {
        OssieDataType.DATE,
        OssieDataType.TIME,
        OssieDataType.DATE_TIME,
        OssieDataType.DATE_TIME_TZ,
    }
)


def resolve_datatype(data_type: str | None, annotations: Mapping[str, str]) -> OssieDataType | None:
    """Map a TMDL ``dataType`` onto an Ossie datatype, or ``None`` if unmappable."""
    if not data_type:
        return None
    mapped = DATA_TYPES.get(data_type)
    if mapped is OssieDataType.DATE_TIME:
        # One TMDL type covers three Power BI ones; the annotation disambiguates.
        return UNDERLYING_TYPES.get(annotations.get(UNDERLYING_TYPE_ANNOTATION, ""), mapped)
    return mapped


# --------------------------------------------------------------------------- #
# Partition source
# --------------------------------------------------------------------------- #

# Power Query string literals escape a quote by doubling it, so a literal runs
# until a quote that is not followed by another one.
_M_STRING = r'"((?:[^"]|"")*)"'
# `Value.NativeQuery(source, "SELECT ...")` -- the second argument is the SQL.
_NATIVE_QUERY = re.compile(r"Value\.NativeQuery\s*\(\s*[^,]+,\s*" + _M_STRING, re.DOTALL)
# `Sql.Database(server, db, [Query="SELECT ..."])` -- a hand-written query.
_QUERY_OPTION = re.compile(r"\bQuery\s*=\s*" + _M_STRING, re.DOTALL)
# `Sql.Database("server", "database")` -- group 2 is the database name.
_SQL_DATABASE = re.compile(r"\bSql\.Database\s*\(\s*" + _M_STRING + r"\s*,\s*" + _M_STRING)
# `Source{[Schema="dbo", Item="Orders"]}` -- the navigation step after a connector.
_SCHEMA_ITEM = re.compile(r"\bSchema\s*=\s*" + _M_STRING + r"\s*,\s*Item\s*=\s*" + _M_STRING)


def resolve_source(source_type: str | None, code: str | None) -> str | None:
    """Recover the physical address behind a partition's M code, if there is one.

    Ossie's ``source`` wants ``database.schema.table`` or a query. M is neither,
    so this recognizes the relational connector patterns and gives up otherwise --
    a CSV read from a local disk has no portable address, and inventing one would
    be worse than the caller's fallback.

    Only SQL-style connectors are covered; Snowflake, OData and the rest fall
    through to ``None`` until a real model makes them worth adding.
    """
    if source_type != "m" or not code:
        return None

    # A native query is already the "query" form Ossie accepts, so prefer it.
    for pattern in (_NATIVE_QUERY, _QUERY_OPTION):
        match = pattern.search(code)
        if match:
            return _unescape(match.group(1)).strip()

    # Otherwise assemble an address from the connector and its navigation step.
    connector = _SQL_DATABASE.search(code)
    database = _unescape(connector.group(2)) if connector else None
    navigation = _SCHEMA_ITEM.search(code)
    if navigation is None:
        return None
    parts = (database, _unescape(navigation.group(1)), _unescape(navigation.group(2)))
    return ".".join(part for part in parts if part)


def _unescape(literal: str) -> str:
    """Undo M's doubled-quote escaping inside a string literal."""
    return literal.replace('""', '"')


# --------------------------------------------------------------------------- #
# Custom extensions
# --------------------------------------------------------------------------- #


def pack_extension(**entries: Any) -> list[OssieCustomExtension] | None:
    """Bundle Power BI metadata that Ossie has no field for into one extension.

    Empty values are dropped, so callers can pass everything an object might
    carry without checking first. Returns ``None`` when nothing is left, which is
    what the models want for an absent optional list.
    """
    data = {key: _jsonable(value) for key, value in entries.items() if not _empty(value)}
    if not data:
        return None
    # `data` is typed as a string in the schema, not an object -- so this is JSON
    # inside JSON. Compact, since nobody reads it in the YAML anyway.
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return [OssieCustomExtension(vendor_name=POWER_BI_VENDOR, data=payload)]


def non_default(value: Any, default: Any) -> Any:
    """Return *value*, or ``None`` when it still holds its TMDL default.

    Keeps extensions down to what the author actually set: every column carries
    ``isNullable``, but only the rare ``isNullable: false`` is worth recording.
    """
    return None if value == default else value


def extras(obj: Any) -> dict[str, Any]:
    """The parser's leftover buckets, so unmapped TMDL survives into the output."""
    return {
        "extra_properties": obj.extra_properties,
        "extra_flags": obj.extra_flags,
        "extra_blocks": obj.extra_blocks,
    }


def _empty(value: Any) -> bool:
    """Whether a value carries no information worth serializing.

    ``False`` is the default of every TMDL flag and so says nothing, but a
    number never counts as empty -- ``ordinal: 0`` is information.
    """
    if value is None or value is False:
        return True
    return isinstance(value, (str, list, tuple, dict, set, frozenset)) and not value


def _jsonable(value: Any) -> Any:
    """Convert parser objects into something ``json.dumps`` accepts.

    Dataclasses become dicts without their empty fields, and the parser's sets
    become sorted lists so repeated runs produce identical output.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in dataclass_fields(value)
            if not _empty(getattr(value, item.name))
        }
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


def deduplicate(name: str, taken: set[str]) -> str:
    """Make *name* unique against *taken*, recording the result.

    Ossie requires unique names within each collection and the upstream validator
    enforces it, so a generated name must never collide.
    """
    unique = name
    counter = 2
    while unique in taken:
        unique = f"{name}_{counter}"
        counter += 1
    taken.add(unique)
    return unique
