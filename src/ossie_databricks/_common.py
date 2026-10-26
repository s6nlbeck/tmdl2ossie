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

"""Shared constants, YAML I/O and typed accessors.

Python port of ``OssieConverterCommon.java`` and the exception/notice types of
``OssieConverter.java`` from ``vendor/ossie-converters-main/databricks/java``.
Only what the export direction needs is ported; the stash *writer* belongs to the
import direction and is left out.

Java semantics are kept where they decide behavior: regexes run with ``re.ASCII``
(Java's ``\\w``/``\\b`` are ASCII), ``String.trim()`` strips only ASCII control
characters and space, and values are stringified the way Java's ``toString()``
would (``true``, not ``True``) because they end up in names and messages.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yaml

# -- constants ---------------------------------------------------------------

OSSIE_VERSION = "0.2.0.dev0"
MV_VERSION = "1.1"
VENDOR = "DATABRICKS"
DIALECT_DATABRICKS = "DATABRICKS"
DIALECT_ANSI = "ANSI_SQL"
# Ossie's portable, ANSI-SQL-compatible dialect; treated as an ANSI_SQL-equivalent fallback.
DIALECT_OSSIE_SQL = "OSSIE_SQL_2026"
SYNONYM_LIMIT = 10
STASH_SOURCE_KEY = "source_dataset"
CARD_ONE_TO_MANY = "one_to_many"
CARD_MANY_TO_ONE = "many_to_one"
MAX_JOIN_NODES = 200

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$", re.ASCII)
SELECT_WITH_RE = re.compile(r"^(select|with)\b", re.IGNORECASE | re.ASCII)


# -- errors and notices --------------------------------------------------------


class ConversionError(Exception):
    """A conversion that cannot produce a valid result (Java ``ConversionException``).

    ``kind`` is one of ``INVALID_INPUT``, ``UNSUPPORTED_VERSION``,
    ``NOT_REPRESENTABLE`` (the default) or ``INTERNAL_ERROR``; ``reason`` is the
    short form of the message, ``str(error)`` the full one.
    """

    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    NOT_REPRESENTABLE = "NOT_REPRESENTABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        kind: str = NOT_REPRESENTABLE,
        reason: str | None = None,
        unsupported_version: str | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.reason = message if reason is None else reason
        self.unsupported_version = unsupported_version

    @classmethod
    def invalid_input(cls, message: str, reason: str | None = None) -> ConversionError:
        return cls(message, kind=cls.INVALID_INPUT, reason=reason)

    @classmethod
    def unsupported(cls, message: str, version: str) -> ConversionError:
        return cls(message, kind=cls.UNSUPPORTED_VERSION, unsupported_version=version)

    @classmethod
    def internal(cls, message: str) -> ConversionError:
        return cls(message, kind=cls.INTERNAL_ERROR)


class Notices:
    """Features dropped during conversion, as ``[scope] message`` lines."""

    def __init__(self) -> None:
        self._messages: list[str] = []

    def warn(self, scope: str, message: str) -> None:
        self._messages.append(f"[{scope}] {message}")

    def to_list(self) -> list[str]:
        return list(self._messages)


@dataclass(frozen=True, slots=True)
class Result:
    """The output YAML plus the notices raised while producing it."""

    yaml: str
    notices: list[str] = field(default_factory=list)


# -- Java value semantics ------------------------------------------------------

# Java's String.trim() removes code points <= U+0020 only, unlike str.strip().
_JAVA_TRIM = "".join(chr(code) for code in range(0x21))


def trim(text: str) -> str:
    return text.strip(_JAVA_TRIM)


def jstr(value: Any) -> str | None:
    """``String.valueOf`` for parsed YAML values; ``None`` stays ``None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return jlist(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{jstr(k)}={jstr(v)}" for k, v in value.items()) + "}"
    return str(value)


def jcat(value: Any) -> str:
    """A value as Java string concatenation renders it (``null`` for a missing one)."""
    text = jstr(value)
    return "null" if text is None else text


def jlist(values: Iterable[Any]) -> str:
    """``List.toString()``: ``[a, b]``."""
    return "[" + ", ".join(jcat(value) for value in values) + "]"


def java_type_name(value: Any) -> str:
    """``getClass().getSimpleName()`` of the object SnakeYAML would have produced."""
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, int):
        return "Integer"
    if isinstance(value, float):
        return "Double"
    if isinstance(value, list):
        return "ArrayList"
    if isinstance(value, dict):
        return "LinkedHashMap"
    if isinstance(value, (date, datetime)):
        return "Date"
    return type(value).__name__


def _java_isspace(char: str) -> bool:
    # Character.isWhitespace excludes the non-breaking spaces that str.isspace() includes.
    return char.isspace() and char not in "   "


# -- YAML I/O ------------------------------------------------------------------


class _Yaml12Loader(yaml.SafeLoader):
    """Safe loader with YAML 1.2 booleans and duplicate-key rejection.

    Only ``true``/``false`` are booleans, not the YAML 1.1 ``on``/``off``/``yes``/
    ``no`` -- a Metric View join's bare ``on:`` key must stay a string. Every other
    implicit type matches SnakeYAML's (also YAML 1.1) resolver.
    """

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in seen
                except TypeError:  # unhashable key; the base class reports it
                    continue
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping", node.start_mark,
                        f"found duplicate key {key!r}", key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


_Yaml12Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_Yaml12Loader)  # noqa: S506 -- a SafeLoader subclass


def parse_yaml(text: str) -> Any:
    """Parse YAML text into plain values (YAML 1.2 booleans, matching the converter)."""
    try:
        return load_yaml(text)
    except Exception as error:
        message = f"failed to parse YAML: {error}"
        raise ConversionError.invalid_input(message, message) from error


def dump_yaml(value: Any) -> str:
    """Serialize to YAML. PyYAML quotes every string that would re-read as another type."""
    return yaml.safe_dump(value, default_flow_style=False, sort_keys=False, allow_unicode=True)


# -- typed accessors over parsed YAML ------------------------------------------


def as_map(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def str_list(value: Any) -> list[str]:
    return [jstr(item) for item in as_list(value) if item is not None]


def is_simple_identifier(expr: Any) -> bool:
    return isinstance(expr, str) and IDENTIFIER_RE.match(trim(expr)) is not None


# -- literal-aware rewriting ---------------------------------------------------


def _literal_spans(sql: str) -> list[tuple[int, int]]:
    """Offsets of every span that is not SQL code: quoted text and comments."""
    spans = []
    i, length = 0, len(sql)
    while i < length:
        char = sql[i]
        skip_to = -1
        if char in "'\"`":
            skip_to = _end_of_quoted(sql, i, char)
        elif char == "-" and i + 1 < length and sql[i + 1] == "-":
            newline = sql.find("\n", i)
            skip_to = length if newline < 0 else newline
        elif char == "/" and i + 1 < length and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            skip_to = length if end < 0 else end + 2
        if skip_to < 0:
            i += 1
            continue
        spans.append((i, skip_to))
        i = skip_to
    return spans


def _end_of_quoted(sql: str, start: int, quote: str) -> int:
    """Index just past the quoted span starting at *start*; handles doubled-quote escapes."""
    i, length = start + 1, len(sql)
    while i < length:
        char = sql[i]
        if char == "\\" and quote != "`" and i + 1 < length:
            i += 2
            continue
        if char == quote:
            # A doubled quote is an escaped quote, not the end of the span.
            if i + 1 < length and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    # Unterminated literal: treat the remainder as part of the span rather than rewriting it.
    return length


def replace_outside_literals(
    sql: str, pattern: re.Pattern[str], replacement: str | Callable[[re.Match[str]], str]
) -> str:
    """Apply *pattern* -> *replacement* to the SQL code only, never inside literals or comments.

    A measure such as ``SUM(IF(source.region = 'source.us', amt, 0))`` must not have
    its literal rewritten, or the predicate -- and the measure's value -- changes.
    The replacement is inserted literally, never read as a template.
    """
    replacer = replacement if callable(replacement) else (lambda _match: replacement)
    out = []
    code_start = 0
    for start, end in _literal_spans(sql):
        out.append(pattern.sub(replacer, sql[code_start:start]))
        out.append(sql[start:end])
        code_start = end
    out.append(pattern.sub(replacer, sql[code_start:]))
    return "".join(out)


def find_outside_literals(sql: str, pattern: re.Pattern[str]) -> bool:
    """Whether *pattern* matches the SQL code of *sql*, ignoring literals and comments."""
    spans = _literal_spans(sql)
    if not spans:
        return pattern.search(sql) is not None
    # Blank the spans rather than removing them, so token boundaries survive.
    code = list(sql)
    for start, end in spans:
        code[start:end] = " " * (end - start)
    return pattern.search("".join(code)) is not None


def qualifier_chain_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """Match a maximal dotted run of *names* qualifying a column (``parent.child.``).

    Group 1 holds the run without its trailing dot. The whole run is matched at
    once so a rewrite can never re-match a qualifier it has just produced.
    """
    names = list(names)
    if not names:
        return None
    # Longest first: alternation is ordered, so `nation_x` must never match as `nation`.
    alternation = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    one = f"(?:{alternation})"
    return re.compile(rf"\b({one}(?:\.{one})*)\.", re.ASCII)


def rewrite_qualifiers(
    expr: str, chain: re.Pattern[str] | None, resolve: Callable[[str], str | None]
) -> str:
    """Rewrite each qualifier run by mapping its LAST name through *resolve*."""
    if chain is None:
        return expr

    def replace(match: re.Match[str]) -> str:
        leaf = last_identifier(match.group(1))
        resolved = None if leaf is None else resolve(leaf)
        return match.group(0) if resolved is None else resolved + "."

    return replace_outside_literals(expr, chain, replace)


# -- validation helpers --------------------------------------------------------


def require(obj: dict[str, Any], key: str, what: str) -> Any:
    """A present, non-null value; an empty/whitespace-only string is rejected too."""
    if obj.get(key) is None:
        raise ConversionError(f"{what} is missing required '{key}'")
    value = obj[key]
    if isinstance(value, str) and not trim(value):
        raise ConversionError(f"{what} has an empty '{key}'")
    return value


def require_str(obj: dict[str, Any], key: str, what: str) -> str:
    value = require(obj, key, what)
    if isinstance(value, str):
        return value
    raise ConversionError(f"{what}: '{key}' must be a string, got {java_type_name(value)}")


def validate_source(source: Any, dataset_name: str) -> str:
    text = "" if source is None else trim(jstr(source))
    if not text:
        raise ConversionError(f"Dataset '{dataset_name}': missing/empty 'source'")
    if SELECT_WITH_RE.match(text):
        return text
    parts = text.split(".")
    if len(parts) == 3 and all(part and not any(map(_java_isspace, part)) for part in parts):
        return text
    raise ConversionError(
        f"Dataset '{dataset_name}': source '{jcat(source)}' must be a 3-part "
        "catalog.schema.table identifier or a SELECT/WITH subquery"
    )


def pick_expression(osi_expression: Any, scope: str) -> str | None:
    """The DATABRICKS expression, else ANSI_SQL, else OSSIE_SQL_2026; ``None`` if none exists."""
    if osi_expression is None:
        return None
    if not isinstance(osi_expression, dict):
        reason = f"{scope}: 'expression' must be a mapping"
        raise ConversionError.invalid_input(reason, reason)
    dialect_values = osi_expression.get("dialects")
    if dialect_values is None:
        return None
    if not isinstance(dialect_values, list):
        reason = f"{scope}: 'expression.dialects' must be a list"
        raise ConversionError.invalid_input(reason, reason)
    dialects: dict[str | None, Any] = {}
    for index, entry in enumerate(dialect_values):
        if not isinstance(entry, dict):
            reason = f"{scope}: 'expression.dialects[{index}]' must be a mapping"
            raise ConversionError.invalid_input(reason, reason)
        dialects[jstr(entry.get("dialect"))] = entry.get("expression")
    if truthy(dialects.get(DIALECT_DATABRICKS)):
        chosen = dialects[DIALECT_DATABRICKS]
    elif truthy(dialects.get(DIALECT_ANSI)):
        chosen = dialects[DIALECT_ANSI]
    else:
        chosen = dialects.get(DIALECT_OSSIE_SQL)
    if chosen is not None and not isinstance(chosen, str):
        raise ConversionError(f"expression must be a string, got {java_type_name(chosen)}")
    return chosen


def truthy(value: Any) -> bool:
    """Emptiness test for optional values: None, "", [], {}, 0 and false are absent."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, list, dict)):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value != 0
    return True


def synonyms_of(ai_context: Any) -> list[str]:
    return str_list(ai_context.get("synonyms")) if isinstance(ai_context, dict) else []


def merge_description(description: Any, ai_context: Any) -> str | None:
    desc = jstr(description)
    if isinstance(ai_context, str) and trim(ai_context):
        return f"{desc}\n{ai_context}" if desc else ai_context
    return desc


def _custom_extensions(obj: dict[str, Any]) -> list[dict[str, Any]]:
    value = obj.get("custom_extensions")
    if value is None:
        return []
    if not isinstance(value, list):
        reason = "'custom_extensions' must be a list"
        raise ConversionError.invalid_input(reason, reason)
    for index, extension in enumerate(value):
        if not isinstance(extension, dict):
            reason = f"'custom_extensions[{index}]' must be a mapping"
            raise ConversionError.invalid_input(reason, reason)
    return value


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate field '{key}'")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise ValueError(f"Non-standard token '{name}'")


def read_stash(obj: dict[str, Any], notices: Notices, scope: str) -> dict[str, Any]:
    """The first DATABRICKS ``custom_extensions`` payload as a dict (``_v`` removed)."""
    stash = None
    saw_extra = False
    for extension in _custom_extensions(obj):
        if jstr(extension.get("vendor_name")) != VENDOR:
            continue
        if stash is not None:
            # First wins, but a dropped entry must not vanish silently.
            saw_extra = True
            continue
        data = extension.get("data")
        if data is not None and not isinstance(data, str):
            reason = "DATABRICKS custom_extensions data must be a string"
            raise ConversionError.invalid_input(reason, reason)
        if not data:
            data = "{}"
        try:
            # Strict like Jackson: duplicate keys, trailing tokens and NaN are errors.
            parsed = json.loads(data, object_pairs_hook=_strict_json_object, parse_constant=_reject_constant)
        except ValueError as error:
            reason = f"DATABRICKS custom_extensions data is not valid JSON: {error}"
            raise ConversionError.invalid_input(reason, reason) from error
        if not isinstance(parsed, dict):
            reason = "DATABRICKS custom_extensions data must be a JSON object"
            raise ConversionError.invalid_input(reason, reason)
        parsed.pop("_v", None)
        stash = parsed
    if saw_extra:
        notices.warn(
            scope,
            "found more than one DATABRICKS custom_extensions entry; using the first and ignoring the rest",
        )
    return stash if stash is not None else {}


def foreign_vendor_extensions(obj: dict[str, Any]) -> list[dict[str, Any]]:
    return [ext for ext in _custom_extensions(obj) if jstr(ext.get("vendor_name")) != VENDOR]


def last_identifier(source: Any) -> str | None:
    """Last dotted part of a table reference, backticks stripped: ``a.b.`t``` -> ``t``."""
    if source is None:
        return None
    text = trim(jstr(source))
    last = text[text.rfind(".") + 1 :]
    return last.strip("`")
