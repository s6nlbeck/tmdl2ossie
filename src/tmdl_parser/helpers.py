"""Low-level helpers for reading TMDL text.

This module knows TMDL's *grammar* -- indentation-nested blocks, ``key: value``
properties, bare boolean flags, ``annotation K = V`` lines, ``/// description``
comments and multi-line M/DAX expressions -- but nothing about its *vocabulary*.
Turning a ``table`` / ``column`` / ``measure`` block into a dataclass is the job
of ``parser.py``.

The entry point is :func:`parse_document`, which turns a file's text into a tree
of :class:`TMDLBlock` nodes.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field

TAB_WIDTH = 4
DESCRIPTION_PREFIX = "///"
COMMENT_PREFIX = "//"
ANNOTATION_KEYWORD = "annotation"


class TMDLParseError(ValueError):
    """Raised when a file cannot be read as valid TMDL."""

    def __init__(self, message: str, *, line: int | None = None, path: str | None = None):
        location = ":".join(str(part) for part in (path, line) if part is not None)
        super().__init__(f"{location}: {message}" if location else message)
        self.line = line
        self.path = path


@dataclass(slots=True)
class TMDLBlock:
    """One named TMDL object together with everything written underneath it.

    ``keyword`` is the first word of the header line (``table``, ``column``,
    ``partition``, ...) and ``name`` the rest of it, unquoted. ``expression``
    holds the right-hand side of a header assignment -- the DAX of a calculated
    column, or the source type of a partition (``partition kunden = m``).
    """

    keyword: str
    name: str | None = None
    expression: str | None = None
    description: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    annotations: dict[str, str] = field(default_factory=dict)
    children: list[TMDLBlock] = field(default_factory=list)
    line: int = 0

    def children_of(self, *keywords: str) -> list[TMDLBlock]:
        return [child for child in self.children if child.keyword in keywords]

    def first(self, keyword: str) -> TMDLBlock | None:
        return next((child for child in self.children if child.keyword == keyword), None)


@dataclass(slots=True)
class _Line:
    """A single source line, pre-measured so the block parser can look ahead cheaply."""

    indent: int
    text: str  # fully stripped, used for classification
    raw: str  # original line, needed to keep expression indentation intact
    number: int
    blank: bool


# --------------------------------------------------------------------------- #
# Text-level helpers
# --------------------------------------------------------------------------- #


def indent_of(raw: str) -> int:
    """Count the indentation level of a line. TMDL uses tabs; spaces are tolerated."""
    level = 0
    position = 0
    while position < len(raw):
        char = raw[position]
        if char == "\t":
            level += 1
            position += 1
        elif char == " ":
            run = position
            while run < len(raw) and raw[run] == " ":
                run += 1
            level += (run - position) // TAB_WIDTH
            position = run
        else:
            break
    return level


def unquote_name(text: str) -> str:
    """Strip the single quotes TMDL puts around names containing spaces or specials."""
    text = text.strip()
    if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
        # An escaped quote inside a quoted name is written doubled.
        return text[1:-1].replace("''", "'")
    return text


def find_delimiter(text: str) -> tuple[str | None, int]:
    """Locate the first unquoted ``:`` or ``=`` and report which one came first.

    Quoting matters because names may contain either character
    (``measure 'Margin: net' = ...``). Whichever delimiter appears first decides
    how the line is read: ``formatString: hh:mm`` is a property, ``source = ...``
    an assignment, even though both contain a colon.
    """
    in_quotes = False
    for index, char in enumerate(text):
        if char == "'":
            in_quotes = not in_quotes
        elif not in_quotes and char in ":=":
            return char, index
    return None, -1


def split_header(text: str) -> tuple[str, str | None]:
    """Split a header line into its keyword and name: ``table kunden`` -> ``(table, kunden)``.

    Only the first word is split off, so names with spaces survive intact.
    Multi-word headers such as ``ref table kunden`` keep ``table kunden`` as the
    name; splitting that further is the caller's business.
    """
    parts = text.split(None, 1)
    if not parts:
        raise TMDLParseError("empty header line")
    keyword = parts[0]
    name = unquote_name(parts[1]) if len(parts) > 1 else None
    return keyword, name


def parse_annotation(text: str) -> tuple[str, str]:
    """Read ``annotation PBI_ResultType = Table`` into a key/value pair."""
    body = text[len(ANNOTATION_KEYWORD) :].strip()
    key, _, value = body.partition("=")
    return unquote_name(key.strip()), value.strip()


def parse_bool(value: str, *, default: bool = False) -> bool:
    """TMDL spells booleans ``true``/``false``; anything else keeps the default."""
    normalized = value.strip().lower()
    if normalized in ("true", "1"):
        return True
    if normalized in ("false", "0"):
        return False
    return default


def split_column_reference(text: str) -> tuple[str | None, str]:
    """Split a column reference into ``(table, column)``.

    Handles the spellings TMDL mixes freely: ``verkauefe.Datum``,
    ``'my table'.Datum``, ``verkauefe[Datum]`` and ``'verkauefe'[Datum]``.
    A bare ``Datum`` yields ``(None, "Datum")``.
    """
    text = text.strip()
    # Bracket form: the table part is everything before the opening bracket.
    if text.endswith("]") and "[" in text:
        table, _, column = text.partition("[")
        return (unquote_name(table) or None) if table.strip() else None, unquote_name(column[:-1])
    # Dotted form: split on the first dot outside quotes, since either side may be quoted.
    in_quotes = False
    for index, char in enumerate(text):
        if char == "'":
            in_quotes = not in_quotes
        elif char == "." and not in_quotes:
            return unquote_name(text[:index]), unquote_name(text[index + 1 :])
    return None, unquote_name(text)


# --------------------------------------------------------------------------- #
# Block parsing
# --------------------------------------------------------------------------- #


def parse_document(text: str, *, path: str | None = None) -> TMDLBlock:
    """Parse a whole ``.tmdl`` file into a synthetic root block.

    Top-level objects land in ``root.children``. Entries written at indent 0
    *next to* them -- which is how TMDL serializes the model's own annotations
    and ``ref`` lines -- land directly on the root.
    """
    lines = _scan(text)
    root = TMDLBlock(keyword="", name=path)
    position = _parse_body(lines, 0, 0, root, path)
    # _parse_body only stops early on a dedent, which cannot happen at level 0.
    if position < len(lines):
        raise TMDLParseError("unexpected indentation", line=lines[position].number, path=path)
    return root


def _scan(text: str) -> list[_Line]:
    """Normalize line endings and measure every line once, up front."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for number, raw in enumerate(normalized.split("\n"), start=1):
        stripped = raw.strip()
        lines.append(
            _Line(
                indent=indent_of(raw),
                text=stripped,
                raw=raw.rstrip(),
                number=number,
                blank=not stripped,
            )
        )
    return lines


def _parse_body(lines: list[_Line], position: int, level: int, block: TMDLBlock, path: str | None) -> int:
    """Fill *block* with every entry written at indentation *level*.

    Returns the index of the first line that no longer belongs to the block, so
    the caller can resume where this one stopped.
    """
    docs: list[str] = []
    while position < len(lines):
        line = lines[position]
        if line.blank:
            position += 1
            continue
        if line.indent < level:
            break  # dedent: this line belongs to an ancestor block
        if line.indent > level:
            # Deeper than expected with no header to own it -- the file is malformed,
            # or an expression was indented too shallowly for us to recognize it.
            raise TMDLParseError("unexpected indentation", line=line.number, path=path)

        text = line.text

        # `/// ...` documents the next object; plain `//` is just a comment.
        if text.startswith(DESCRIPTION_PREFIX):
            docs.append(text[len(DESCRIPTION_PREFIX) :].strip())
            position += 1
            continue
        if text.startswith(COMMENT_PREFIX):
            position += 1
            continue

        # Annotations can appear at every level and always belong to the enclosing object.
        if text == ANNOTATION_KEYWORD or text.startswith(ANNOTATION_KEYWORD + " "):
            key, value = parse_annotation(text)
            block.annotations[key] = value
            position += 1
            docs.clear()
            continue

        delimiter, index = find_delimiter(text)

        if delimiter == ":":
            # `key: value` -- a single-line property. Properties never nest.
            block.properties[text[:index].strip()] = text[index + 1 :].strip()
            position += 1
            docs.clear()
            continue

        if delimiter == "=":
            left, right = text[:index].strip(), text[index + 1 :].strip()
            # One word left of the `=` means a property holding an expression
            # (`source = ...`); two or more mean a header (`measure 'X' = ...`).
            if len(left.split(None, 1)) == 1:
                if right:
                    value, position = right, position + 1
                else:
                    # The value continues on the following, deeper-indented lines.
                    value, position = _read_expression(lines, position + 1, level + 1)
                block.properties[left] = value
                docs.clear()
                continue

            child = _open_block(left, line, docs)
            if right:
                child.expression = right
                position += 1
            else:
                # A multi-line expression sits deeper than the object's own
                # properties, which live at level + 1 -- that is what separates
                # the DAX of a measure from the `formatString` line after it.
                child.expression, position = _read_expression(lines, position + 1, level + 2)
            position = _parse_body(lines, position, level + 1, child, path)
            block.children.append(child)
            docs.clear()
            continue

        # No delimiter: either a nested block header or a bare boolean flag.
        # A second word (`table kunden`) or nested content (`dataAccessOptions`)
        # makes it a header; a lone word on its own (`isHidden`) is a flag.
        has_nested_content = _next_content_indent(lines, position + 1) > level
        if len(text.split(None, 1)) > 1 or has_nested_content:
            child = _open_block(text, line, docs)
            position = _parse_body(lines, position + 1, level + 1, child, path)
            block.children.append(child)
        else:
            block.flags.append(text)
            position += 1
        docs.clear()

    return position


def _open_block(header: str, line: _Line, docs: list[str]) -> TMDLBlock:
    """Start a new block from its header line, attaching any pending `///` lines."""
    keyword, name = split_header(header)
    return TMDLBlock(
        keyword=keyword,
        name=name,
        description="\n".join(docs) if docs else None,
        line=line.number,
    )


def _next_content_indent(lines: list[_Line], position: int) -> int:
    """Indentation of the next non-blank line, or -1 at end of file."""
    while position < len(lines):
        if not lines[position].blank:
            return lines[position].indent
        position += 1
    return -1


def _read_expression(lines: list[_Line], position: int, min_indent: int) -> tuple[str, int]:
    """Consume an indented M/DAX block and return it verbatim, minus its indentation.

    Blank lines are kept (DAX is routinely written with them) but do not end the
    block -- only a non-blank line indented above *min_indent* does.
    """
    start = position
    end = position
    while position < len(lines):
        line = lines[position]
        if line.blank:
            position += 1
            continue
        if line.indent < min_indent:
            break
        position += 1
        end = position  # last non-blank line still inside the expression

    if end == start:
        return "", start
    body = "\n".join(line.raw for line in lines[start:end])
    # dedent() strips the indentation shared by all lines, keeping the relative
    # structure of the M or DAX code intact.
    return textwrap.dedent(body).strip("\n"), end
