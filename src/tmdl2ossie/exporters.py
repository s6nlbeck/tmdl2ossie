"""Export targets offered by the UI.

Every target is one :class:`ExportTarget` in :data:`TARGETS`: a label for the
UI, the options it asks the user for, and a function that turns a validated
:class:`~ossie.OssieDocument` into an :class:`ExportArtifact` (file name,
content, MIME type, notes). The UI knows nothing beyond that, so adding a
platform means writing one function and one entry.

Implemented: the Ossie formats themselves, Snowflake, Cube and Honeydew via the
vendored Apache Ossie converters (``vendor/ossie-converters-main/<platform>``), and
Databricks via ``ossie_databricks``, our Python port of the Java converter. A
target whose function raises :class:`NotImplementedError` is shown as "not yet
integrated" rather than as an error -- the way to stub out the next platform.
"""

from __future__ import annotations

import io
import json
import re
import warnings
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml
from ossie import OssieDocument


class ExportError(ValueError):
    """The document is valid Ossie but cannot be expressed on the target platform."""


@dataclass(frozen=True, slots=True)
class ExportOption:
    """A setting the UI asks for before exporting."""

    key: str
    label: str
    help: str = ""
    placeholder: str = ""
    #: ``text`` for free input, ``dataset`` for a choice among the document's
    #: datasets, ``choice`` for one of *choices*.
    kind: str = "text"
    #: ``(value, label)`` pairs for ``kind="choice"``; an empty value means "default".
    choices: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    """A file ready for download."""

    file_name: str
    content: str | bytes
    mime: str = "text/plain"
    #: What the conversion could not carry over, for the user to review.
    notes: list[str] = field(default_factory=list)
    #: Problems that will make the result fail on the target platform.
    warnings: list[str] = field(default_factory=list)
    #: Text preview for binary content such as a ZIP of several files.
    preview: str | None = None


@dataclass(frozen=True, slots=True)
class ExportTarget:
    key: str
    label: str
    description: str
    #: Only ever called with a document that passed validation. The mapping
    #: holds the user's answers to *options*, empty answers left out.
    export: Callable[[OssieDocument, Mapping[str, str]], ExportArtifact]
    options: tuple[ExportOption, ...] = ()


def file_stem(document: OssieDocument) -> str:
    """A file-system-safe stem derived from the document name."""
    stem = re.sub(r"[^\w.-]+", "_", document.name, flags=re.UNICODE).strip("._")
    return stem or "semantic_model"


# --------------------------------------------------------------------------- #
# Ossie
# --------------------------------------------------------------------------- #


def export_ossie_yaml(document: OssieDocument, options: Mapping[str, str]) -> ExportArtifact:
    return ExportArtifact(f"{file_stem(document)}.ossie.yaml", document.to_ossie_yaml(), "application/yaml")


def export_ossie_json(document: OssieDocument, options: Mapping[str, str]) -> ExportArtifact:
    return ExportArtifact(
        f"{file_stem(document)}.ossie.json", document.to_ossie_json(indent=2), "application/json"
    )


# --------------------------------------------------------------------------- #
# Snowflake
# --------------------------------------------------------------------------- #


def export_snowflake(document: OssieDocument, options: Mapping[str, str]) -> ExportArtifact:
    """Snowflake Cortex Analyst semantic model YAML, via the Apache Ossie converter.

    The converter insists on ``database.schema.table`` (or a query) as source,
    which a Power BI model built on CSV files or a bare table name cannot
    provide. The *database* and *schema* options fill the missing parts in.
    """
    from ossie_snowflake.ossie_to_snowflake import OssieConversionError, convert_ossie_to_snowflake

    data = json.loads(document.to_ossie_json())
    notes = []

    # The converter drops custom_extensions anyway, with one warning per object
    # -- dozens for a Power BI model, all saying the same thing.
    if stripped := _strip_extensions(data, keep_vendor=None):
        notes.append(
            f"Power-BI-Metadaten (`custom_extensions`) an {stripped} Objekten nicht übertragen – "
            "Snowflake hat dafür kein Gegenstück."
        )
    qualified = _qualify_sources(data, options.get("database"), options.get("schema"), _snowflake_identifier)
    if qualified:
        notes.append(f"Quelle ergänzt: {', '.join(qualified)}.")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            content = convert_ossie_to_snowflake(_dump_yaml(data))
        except OssieConversionError as error:
            hint = ""
            if "fully qualified" in str(error):
                hint = " Datenbank und Schema angeben oder die Quelle im Tab *Bearbeiten* voll qualifizieren."
            raise ExportError(f"{error}.{hint}") from error

    notes += list(dict.fromkeys(str(warning.message) for warning in caught))
    return ExportArtifact(f"{file_stem(document)}.snowflake.yaml", content, "application/yaml", notes)


_IDENTIFIER_PART = re.compile(r'"(?:[^"]|"")*"|`(?:[^`]|``)*`|[^.]+')
_QUERY = re.compile(r"^\s*\(*\s*(SELECT|WITH)\b", re.IGNORECASE)


def _qualify_sources(
    data: dict[str, Any], database: str | None, schema: str | None, identifier: Callable[[str], str]
) -> list[str]:
    """Prefix bare and two-part dataset sources with *database* / *schema* in place.

    Returns a ``old → new`` line per changed source. Sources that are queries or
    already have three parts are left alone, as is anything the options cannot
    complete -- the converter then reports it with its own, precise message.
    """
    changes = []
    for dataset in data.get("datasets") or []:
        source = str(dataset.get("source", "")).strip()
        if not source or _QUERY.match(source):
            continue
        parts = _IDENTIFIER_PART.findall(source)
        if len(parts) == 1 and database and schema:
            qualified = ".".join((identifier(database), identifier(schema), identifier(parts[0])))
        elif len(parts) == 2 and database:
            qualified = f"{identifier(database)}.{source}"
        else:
            continue
        dataset["source"] = qualified
        changes.append(f"`{source}` → `{qualified}`")
    return changes


_SNOWFLAKE_UNQUOTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_SNOWFLAKE_QUOTED = re.compile(r'^"(?:[^"]|"")+"$')
_DATABRICKS_UNQUOTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DATABRICKS_QUOTED = re.compile(r"^`(?:[^`]|``)+`$")


def _snowflake_identifier(name: str) -> str:
    """Quote a name unless Snowflake accepts it as it is (umlauts, spaces, ...)."""
    name = name.strip()
    if _SNOWFLAKE_UNQUOTED.match(name) or _SNOWFLAKE_QUOTED.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _databricks_identifier(name: str) -> str:
    """Backtick a name unless Databricks accepts it bare. Whitespace stays impossible:
    the converter rejects any source part containing it, quoted or not."""
    name = name.strip()
    if _DATABRICKS_UNQUOTED.match(name) or _DATABRICKS_QUOTED.match(name):
        return name
    return "`" + name.replace("`", "``") + "`"


def _strip_extensions(node: Any, *, keep_vendor: str | None) -> int:
    """Remove ``custom_extensions`` entries in place, except *keep_vendor*'s.

    Returns the number of objects that lost at least one entry.
    """
    count = 0
    if isinstance(node, dict):
        extensions = node.get("custom_extensions")
        if isinstance(extensions, list):
            kept = [e for e in extensions if isinstance(e, dict) and e.get("vendor_name") == keep_vendor]
            if len(kept) < len(extensions):
                count += 1
            if kept:
                node["custom_extensions"] = kept
            else:
                del node["custom_extensions"]
        for value in node.values():
            count += _strip_extensions(value, keep_vendor=keep_vendor)
    elif isinstance(node, list):
        for item in node:
            count += _strip_extensions(item, keep_vendor=keep_vendor)
    return count


def _dump_yaml(data: dict[str, Any]) -> str:
    return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)


# --------------------------------------------------------------------------- #
# Databricks
# --------------------------------------------------------------------------- #


def export_databricks(document: OssieDocument, options: Mapping[str, str]) -> ExportArtifact:
    """Databricks Unity Catalog Metric View (YAML v1.1), via ``ossie_databricks``.

    A Metric View is built around one fact table, with every other dataset joined
    below it, and has a single flat namespace for dimensions and measures. Three
    things every Power BI star schema runs into are therefore fixed up first:

    - sources lacking catalog/schema get them from the options (as for Snowflake);
    - tables with no relationship (measure tables, parameter tables) cannot be
      joined and are left out -- otherwise they would count as a second fact;
    - a field whose name repeats one already taken -- typically the key on the
      dimension side, `kunden.KundenID` next to `verkauefe.KundenID` -- is renamed
      to `<dataset>_<field>`. Only the name changes; expression, keys and
      relationships keep pointing at the physical column.
    """
    from ossie_databricks import ConversionError, convert_ossie_to_metric_view

    data = json.loads(document.to_ossie_json())
    notes = []
    if stripped := _strip_extensions(data, keep_vendor="DATABRICKS"):
        notes.append(
            f"Power-BI-Metadaten (`custom_extensions`) an {stripped} Objekten nicht übertragen – "
            "Databricks hat dafür kein Gegenstück."
        )
    qualified = _qualify_sources(data, options.get("catalog"), options.get("schema"), _databricks_identifier)
    if qualified:
        notes.append(f"Quelle ergänzt: {', '.join(qualified)}.")

    fact = options.get("fact") or _single_fact(data)
    if fact:
        if dropped := _drop_unjoinable(data, fact):
            notes.append(
                f"Ohne Beziehung zur Faktentabelle `{fact}` und daher weggelassen: "
                + ", ".join(f"`{name}`" for name in dropped)
                + "."
            )
        if renamed := _rename_colliding_fields(data, fact):
            notes.append(f"Umbenannt, weil Metric Views eindeutige Namen verlangen: {', '.join(renamed)}.")

    try:
        result = convert_ossie_to_metric_view(_dump_yaml(data), fact)
    except ConversionError as error:
        hint = ""
        if "multiple candidate fact" in str(error):
            hint = " Faktentabelle oben auswählen."
        elif "3-part" in str(error):
            hint = " Katalog und Schema angeben oder die Quelle im Tab *Bearbeiten* voll qualifizieren."
        raise ExportError(f"{error}{hint}") from error

    notes += _collapse_notices(result.notices)
    return ExportArtifact(f"{file_stem(document)}.metric_view.yaml", result.yaml, "application/yaml", notes)


def _single_fact(data: dict[str, Any]) -> str | None:
    """The one connected dataset no relationship points at, if there is exactly one.

    Unlike the converter's own choice this ignores datasets without any
    relationship, so a stray measure table does not count as a second fact.
    """
    relationships = data.get("relationships") or []
    if not relationships:
        datasets = data.get("datasets") or []
        return datasets[0]["name"] if len(datasets) == 1 else None
    connected = {rel["from"] for rel in relationships} | {rel["to"] for rel in relationships}
    targets = {rel["to"] for rel in relationships}
    roots = [d["name"] for d in data.get("datasets") or [] if d["name"] in connected - targets]
    return roots[0] if len(roots) == 1 else None


def _drop_unjoinable(data: dict[str, Any], fact: str) -> list[str]:
    """Remove datasets the fact cannot reach through relationships; return their names."""
    relationships = data.get("relationships") or []
    neighbours: dict[str, set[str]] = {}
    for rel in relationships:
        neighbours.setdefault(rel["from"], set()).add(rel["to"])
        neighbours.setdefault(rel["to"], set()).add(rel["from"])
    reachable, frontier = {fact}, [fact]
    while frontier:
        for neighbour in neighbours.get(frontier.pop(), ()):
            if neighbour not in reachable:
                reachable.add(neighbour)
                frontier.append(neighbour)
    datasets = data.get("datasets") or []
    if not any(d["name"] == fact for d in datasets):
        return []  # an unknown fact is the converter's error to report
    dropped = [d["name"] for d in datasets if d["name"] not in reachable]
    if dropped:
        data["datasets"] = [d for d in datasets if d["name"] in reachable]
        if relationships:
            data["relationships"] = [r for r in relationships if r["from"] in reachable and r["to"] in reachable]
    return dropped


def _rename_colliding_fields(data: dict[str, Any], fact: str) -> list[str]:
    """Give every field a name unique among fields and metrics, case-insensitively.

    The fact table keeps its names and metrics keep theirs; joined datasets yield.
    """
    taken = {metric["name"].lower() for metric in data.get("metrics") or []}
    datasets = sorted(data.get("datasets") or [], key=lambda d: d["name"] != fact)  # fact first
    renamed = []
    for dataset in datasets:
        for field_ in dataset.get("fields") or []:
            name = field_["name"]
            if name.lower() in taken and dataset["name"] != fact:
                base = new = f"{dataset['name']}_{name}"
                counter = 2
                while new.lower() in taken:
                    new, counter = f"{base}_{counter}", counter + 1
                field_["name"] = new
                renamed.append(f"`{dataset['name']}.{name}` → `{new}`")
            taken.add(field_["name"].lower())
    return renamed


def _collapse_notices(notices: list[str], threshold: int = 3) -> list[str]:
    """Fold notices that repeat one message for many objects into a single line."""
    groups: dict[str, list[str]] = {}
    for notice in notices:
        match = re.match(r"\[(.*?)\] (.*)", notice, re.DOTALL)
        scope, message = match.groups() if match else ("", notice)
        groups.setdefault(message, []).append(scope)
    collapsed = []
    for message, scopes in groups.items():
        if len(scopes) < threshold:
            collapsed += [f"[{scope}] {message}" if scope else message for scope in scopes]
            continue
        shown = ", ".join(scopes[:4]) + (", …" if len(scopes) > 4 else "")
        collapsed.append(f"{message} ({len(scopes)}×: {shown})")
    return collapsed


# --------------------------------------------------------------------------- #
# Cube
# --------------------------------------------------------------------------- #


def export_cube(document: OssieDocument, options: Mapping[str, str]) -> ExportArtifact:
    """Cube data model (``model/cubes/*.yml`` + ``model/views/*.yml``) as a ZIP.

    Uses the vendored Apache Ossie converter. Its main pitfall with Power BI input
    is reported as a warning rather than an error: Cube requires a primary key on
    every cube that declares a join, and fact tables rarely have one in Power BI.
    The converter still writes the model; it just will not compile until the key
    is added (``primary_key`` in the YAML tab).
    """
    from ossie_cube import ConversionError, convert_ossie_to_cube

    data = json.loads(document.to_ossie_json())
    notes = []
    # The converter would park them under meta.ossie for a lossless round trip --
    # including the full M code with local file paths. Not wanted in a Cube repo.
    if stripped := _strip_extensions(data, keep_vendor="CUBE"):
        notes.append(f"Power-BI-Metadaten (`custom_extensions`) an {stripped} Objekten nicht übertragen.")
    dialect = options.get("dialect")
    identifier = _databricks_identifier if dialect in {"DATABRICKS", "BIGQUERY"} else _snowflake_identifier
    if qualified := _qualify_sources(data, options.get("database"), options.get("schema"), identifier):
        notes.append(f"Quelle ergänzt: {', '.join(qualified)}.")

    try:
        files, issues = convert_ossie_to_cube(_dump_yaml(data), dialect=dialect, base_cube=options.get("fact"))
    except ConversionError as error:
        raise ExportError(str(error)) from error

    warnings_, rest = [], []
    for issue in issues.issues:
        (warnings_ if "will not compile" in issue.detail else rest).append(str(issue))
    return _zip_artifact(f"{file_stem(document)}.cube.zip", files, notes + rest, warnings_)


def _zip_artifact(file_name: str, files: Mapping[str, str], notes: list[str], warnings_: list[str]) -> ExportArtifact:
    """Pack a converter's ``{relative path: text}`` output, with a text preview.

    The paths derive from dataset and metric names, which the user can edit
    freely, so anything escaping the archive root is refused.
    """
    paths = sorted(files)
    for path in paths:
        parts = PurePosixPath(path.replace("\\", "/")).parts
        if path.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
            raise ExportError(f"Unzulässiger Dateipfad in der Ausgabe: {path!r} – Namen im Tab *Bearbeiten* prüfen.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.writestr(path, files[path])
    preview = "\n".join(f"# ── {path} ──\n{files[path]}" for path in paths)
    return ExportArtifact(file_name, buffer.getvalue(), "application/zip", notes, warnings_, preview)


# --------------------------------------------------------------------------- #
# Honeydew
# --------------------------------------------------------------------------- #

# Honeydew runs on a SQL warehouse; anything else is not an expression it can use.
_SQL_DIALECTS = {"ANSI_SQL", "OSSIE_SQL_2026", "SNOWFLAKE", "DATABRICKS", "BIGQUERY"}


def export_honeydew(document: OssieDocument, options: Mapping[str, str]) -> ExportArtifact:
    """Honeydew workspace (``workspace.yml`` + ``schema/<entity>/...``) as a ZIP.

    Uses the vendored Apache Ossie converter. Where a field or metric has no
    ANSI_SQL expression, that converter falls back to the *first* dialect with
    only a warning -- for a Power BI model that is DAX, which would land in
    Honeydew as SQL. Non-SQL dialects are therefore removed first, and what is
    left without any expression is dropped with a note.
    """
    from ossie_honeydew.converter import HoneydewConversionError, convert_ossie_to_honeydew

    data = json.loads(document.to_ossie_json())
    notes = []
    # Like Cube, the converter would keep them (in Honeydew `metadata`), M code included.
    if stripped := _strip_extensions(data, keep_vendor="HONEYDEW"):
        notes.append(f"Power-BI-Metadaten (`custom_extensions`) an {stripped} Objekten nicht übertragen.")
    if dropped := _keep_sql_expressions(data):
        notes.append(f"Ohne SQL-Ausdruck (z. B. reines DAX) weggelassen: {', '.join(dropped)}.")
    if qualified := _qualify_sources(data, options.get("database"), options.get("schema"), _snowflake_identifier):
        notes.append(f"Quelle ergänzt: {', '.join(qualified)}.")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            files = convert_ossie_to_honeydew(_dump_yaml(data))
        except HoneydewConversionError as error:
            raise ExportError(str(error)) from error

    warnings_, rest = [], []
    for message in dict.fromkeys(str(warning.message) for warning in caught):
        if "no entity reference found" in message:
            # The metric lands on an arbitrary entity, and its columns will not resolve there.
            warnings_.append(
                f"{message} – Spalten im Ausdruck mit dem Tabellennamen qualifizieren, z. B. `verkauefe.Umsatz`."
            )
        else:
            rest.append(message)
    return _zip_artifact(f"{file_stem(document)}.honeydew.zip", files, notes + rest, warnings_)


def _keep_sql_expressions(data: dict[str, Any]) -> list[str]:
    """Strip non-SQL dialects from every field and metric; drop what is left empty.

    Returns ``dataset.field`` / ``metric`` labels of what was dropped.
    """
    def sql_only(item: dict[str, Any]) -> bool:
        dialects = [d for d in item["expression"]["dialects"] if d.get("dialect") in _SQL_DIALECTS]
        item["expression"]["dialects"] = dialects
        return bool(dialects)

    dropped = []
    for dataset in data.get("datasets") or []:
        kept = []
        for field_ in dataset.get("fields") or []:
            if sql_only(field_):
                kept.append(field_)
            else:
                dropped.append(f"`{dataset['name']}.{field_['name']}`")
        if "fields" in dataset:
            dataset["fields"] = kept
    if metrics := data.get("metrics"):
        kept = []
        for metric in metrics:
            if sql_only(metric):
                kept.append(metric)
            else:
                dropped.append(f"`{metric['name']}`")
        data["metrics"] = kept
    return dropped


TARGETS: dict[str, ExportTarget] = {
    target.key: target
    for target in [
        ExportTarget("ossie_yaml", "Ossie YAML", "Das Ossie-Dokument selbst, als YAML.", export_ossie_yaml),
        ExportTarget("ossie_json", "Ossie JSON", "Das Ossie-Dokument selbst, als JSON.", export_ossie_json),
        ExportTarget(
            "snowflake",
            "Snowflake",
            "Cortex-Analyst-Semantikmodell (YAML), erzeugt mit dem Apache-Ossie-Konverter. "
            "Felder und Metriken ohne SQL-Ausdruck (z. B. reines DAX) werden übersprungen.",
            export_snowflake,
            options=(
                ExportOption(
                    "database",
                    "Datenbank",
                    "Ergänzt Quellen, denen die Datenbank fehlt.",
                    placeholder="ANALYTICS",
                ),
                ExportOption(
                    "schema",
                    "Schema",
                    "Ergänzt Quellen, die nur aus dem Tabellennamen bestehen.",
                    placeholder="RETAIL",
                ),
            ),
        ),
        ExportTarget(
            "databricks",
            "Databricks",
            "Unity Catalog Metric View (YAML), erzeugt mit `ossie_databricks`, dem Python-Port des "
            "Apache-Ossie-Konverters. Felder und Metriken ohne SQL-Ausdruck (z. B. reines DAX) werden übersprungen.",
            export_databricks,
            options=(
                ExportOption("catalog", "Katalog", "Ergänzt Quellen, denen der Katalog fehlt.", placeholder="main"),
                ExportOption(
                    "schema", "Schema", "Ergänzt Quellen, die nur aus dem Tabellennamen bestehen.", placeholder="retail"
                ),
                ExportOption(
                    "fact",
                    "Faktentabelle",
                    "Die Tabelle, um die der Metric View gebaut wird. Automatisch: die einzige Tabelle, "
                    "auf die keine Beziehung zeigt.",
                    kind="dataset",
                ),
            ),
        ),
        ExportTarget(
            "cube",
            "Cube",
            "Cube-Datenmodell (ein Cube pro Dataset plus ein View), als ZIP mit `model/cubes/` und `model/views/`, "
            "erzeugt mit dem Apache-Ossie-Konverter. Felder und Metriken ohne SQL-Ausdruck (z. B. reines DAX) "
            "werden übersprungen.",
            export_cube,
            options=(
                ExportOption(
                    "dialect",
                    "SQL-Dialekt",
                    "Welcher Ausdruck bevorzugt wird, wenn ein Feld mehrere Dialekte hat; "
                    "ANSI SQL ist immer der Rückfall.",
                    kind="choice",
                    choices=(("", "ANSI SQL"), ("SNOWFLAKE", "Snowflake"), ("DATABRICKS", "Databricks"),
                             ("BIGQUERY", "BigQuery")),
                ),
                ExportOption("database", "Datenbank", "Optional: ergänzt Quellen, denen die Datenbank fehlt."),
                ExportOption("schema", "Schema", "Optional: ergänzt Quellen, die nur aus dem Tabellennamen bestehen."),
                ExportOption(
                    "fact",
                    "Basis des Views",
                    "Die Tabelle, von der aus der View joint. Automatisch: die Tabelle, auf die keine Beziehung zeigt.",
                    kind="dataset",
                ),
            ),
        ),
        ExportTarget(
            "honeydew",
            "Honeydew",
            "Honeydew-Workspace (`workspace.yml` und je Entity ein Ordner unter `schema/`), als ZIP, erzeugt mit dem "
            "Apache-Ossie-Konverter. Felder und Metriken ohne SQL-Ausdruck (z. B. reines DAX) werden weggelassen.",
            export_honeydew,
            options=(
                ExportOption("database", "Datenbank", "Optional: ergänzt Quellen, denen die Datenbank fehlt."),
                ExportOption("schema", "Schema", "Optional: ergänzt Quellen, die nur aus dem Tabellennamen bestehen."),
            ),
        ),
    ]
}
