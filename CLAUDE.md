# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

Convert a Power BI semantic model (TMDL) into the standardized Apache Ossie semantic format:

1. Parse the `.tmdl` files
2. Map the parsed dataclasses directly onto the Ossie pydantic models
3. Validate and emit Ossie-conformant YAML
4. Hand off to existing Ossie → 3rd-party-semantic converters

An earlier plan had an intermediate application-specific JSON format between steps 1 and 2. **That step was dropped** — it bought nothing over mapping straight from `tmdl_parser.datamodels` to `ossie.models`.

**`docs/tmdl-to-ossie-mapping.md` is the specification for the generation pack** — it fixes which TMDL construct becomes which Ossie element, the datatype table, the filtering rules, and what gets dropped. Read it before writing or changing mapping code, and update it when a rule changes.

## State

The TMDL parser, Ossie validation, the TMDL → Ossie mapping and a Streamlit UI are implemented. Export targets: Ossie YAML/JSON, Snowflake, Cube and Honeydew through the vendored Ossie converters, Databricks through `src/ossie_databricks/` (our Python port of the vendored Java converter). dbt was evaluated and deliberately left out (its converter emits `semantic_manifest.json`, not project YAML, and cannot be a workspace member).

`src/tmdl2ossie/mapping.py` converts a `TMDLSemanticModel` into an `OssieDocument` (`convert(model, *, name=None)` → `ConversionResult`, with `mapping_helpers.py` holding the data type table, M source resolution and extension packing). It never raises on awkward input if it can avoid it — unexpressible constructs are skipped and reported in `ConversionResult.warnings`, so one bad relationship does not cost the whole model. `MappingError` is reserved for a model that cannot produce a document at all. The UI is `src/tmdl2ossie/app.py` (`main()` in `__init__.py` launches it), with the non-UI logic in three modules:

- `loading.py` — untrusted ZIP upload → one `LoadedModel` per `model.tmdl` found. Extracts *only* `.tmdl` files, skips macOS `__MACOSX/` / `._*` resource forks, rejects path traversal, caps inflated size.
- `graph.py` — Ossie dict → Graphviz DOT (ER diagram, rendered in the browser by `st.graphviz_chart`; no Graphviz install needed). Must tolerate hand-edited, invalid documents.
- `exporters.py` — `TARGETS` registry; each target is a function `(OssieDocument, options) -> ExportArtifact` plus the `ExportOption`s the UI asks for. Adding a platform = one function + one entry. Exporters only ever receive a document that passed validation; `ExportError` means "valid Ossie, but not expressible on that platform". Converter `warnings.warn` output is captured into `ExportArtifact.notes`; problems that will make the result fail on the platform go into `ExportArtifact.warnings` (shown above the download). Multi-file outputs are zipped, with `ExportArtifact.preview` holding the text the UI shows. Option kinds: `text`, `choice` (`choices` = value/label pairs), `dataset`.
- `src/ossie_databricks/` — Python port of the **export direction only** of `vendor/ossie-converters-main/databricks/java` (`OssieToMetricView` + the shared parts of `OssieConverterCommon`). The Java original needs Java 21 + Maven, which the app should not depend on. Function names and order mirror the Java files so they can be diffed when upstream changes; keep it that way. Java semantics deliberately kept: regexes with `re.ASCII`, Java-style `trim()`/`toString()` in messages, YAML 1.2 booleans on read (`on:` stays a string). The 45 export tests of `OssieConverterSuite.java`, including both export fixture comparisons, are ported to `tests/test_ossie_databricks.py` (the skipped import-direction tests are listed at its top) — after an upstream update, re-port changed tests first, then the code.
- Databricks export specifics (`export_databricks`): a Metric View is one fact table with everything else joined below it, in one flat namespace. Before converting, the exporter qualifies sources with *Katalog*/*Schema* (backticks; whitespace is impossible), drops datasets the fact cannot reach (Power BI measure/parameter tables would otherwise count as a second fact), and renames fields whose name is already taken to `<dataset>_<field>` — every Power BI star schema has `fact.KundenID` next to `dim.KundenID`, which Databricks rejects. Repeated per-object notices are folded into one line.
- Cube specifics (`export_cube`): the converter returns `{path: yaml}` for `model/cubes/*.yml` + `model/views/*.yml`; the exporter zips it. Foreign `custom_extensions` are stripped first — Cube would otherwise park them losslessly under `meta.ossie`, M code and local file paths included. Cube needs a primary key on every cube that declares a join, and Power BI fact tables rarely have one; the converter still writes the model and reports it, which the exporter surfaces as a warning (fix: `primary_key` in the YAML tab).
- Honeydew specifics (`export_honeydew`): output is a workspace tree (`workspace.yml`, `schema/<entity>/...`), zipped. The converter's expression pick falls back to the *first* dialect when there is no ANSI_SQL (only a `warnings.warn`), which for Power BI means DAX shipped as SQL — so `_keep_sql_expressions` removes non-SQL dialects first and drops what is left empty. A metric whose expression names no dataset is assigned to an arbitrary entity by the converter; that is surfaced as a warning (qualify columns, `verkauefe.Umsatz`). Its file names come straight from metric/dataset names — `_zip_artifact` (shared with Cube) refuses paths escaping the archive.
- Snowflake specifics: the converter (`ossie_snowflake.convert_ossie_to_snowflake`, YAML string in/out) rejects any `source` that is not `db.schema.table` or a SELECT/WITH query — which is every CSV-backed Power BI table, since the mapping falls back to the bare table name. The export therefore takes *Datenbank*/*Schema* options and qualifies bare or two-part sources before converting (quoting non-ASCII names). `custom_extensions` are stripped first, because the converter would otherwise warn once per object. DAX-only fields/metrics are skipped by the converter with a warning.

The app's single source of truth is `st.session_state.doc`, the document as a **plain dict**, not an `OssieDocument` — the pydantic models would silently drop unknown nested keys typed into the YAML editor. Every change goes through `_commit()`, which bumps `rev`; widget keys include `rev` so forms rebuild from the new document. YAML edits are accepted once they pass the pydantic *structure* check; finer errors show in the sidebar instead of blocking the edit.

Tests: `tests/test_parser.py` (grammar + vocabulary), `tests/test_mapping.py` (the rules of the mapping spec), `tests/test_ui.py` (loading, graph, exporters, Streamlit `AppTest`) and `tests/test_ossie_databricks.py`. Parser and mapping tests run against `tests/fixtures/Sales.SemanticModel`, a hand-written TMDL model (tab-indented like a real export) covering what the Retail sample lacks, plus the Retail sample itself. `test_retail_sample_is_fully_understood` asserts that a real export leaves the parser's `extra_*` buckets empty — the lossless invariant used as an early warning for new Power BI constructs. The TMDL parser is pure standard library; the dependencies exist for the Ossie side.

`src/tmdl_parser/` is split in three layers, and changes should respect that split:

- `helpers.py` — TMDL's *grammar* only. `parse_document()` turns file text into a tree of `TMDLBlock` nodes (keyword, name, expression, properties, flags, annotations, children). Knows nothing about tables or measures.
- `parser.py` — TMDL's *vocabulary*. Maps block keywords onto dataclasses and reads a whole `definition/` directory via `parse_semantic_model(path)` (accepts the definition dir, the `.SemanticModel` dir, or a .pbip root).
- `datamodels.py` — plain data holders, a faithful mirror of the file. No normalization; that belongs in packs 2–3.

Two invariants worth preserving:

- **Parsing is lossless.** Unrecognized properties/flags/blocks land in `extra_properties` / `extra_flags` / `extra_blocks` instead of being dropped. Walking a parsed model and asserting those are empty is the quickest way to find TMDL constructs the mapping doesn't cover yet — that is how table-level `showAsVariationsOnly` was found.
- **Field names mirror TMDL** (`lineageTag`, `sourceColumn`), deliberately breaking PEP 8 so objects line up 1:1 with the text they came from.

The two structural rules the block parser relies on, both documented at their implementation in `helpers.py`:

- A line's role is decided by its first unquoted `:` or `=`. One word left of `=` is a property with an expression value (`source = ...`); two or more is a block header (`measure 'X' = ...`, `partition kunden = m` — where the right-hand side is the source *type*, not code).
- A multi-line M/DAX expression is recognized by being indented *deeper than the object's own properties*. That is what separates a measure's DAX from the `formatString` line following it. An expression indented at property level would be misread.

The Retail sample contains **no measures**, no calculated columns, no `isKey` and no SQL-backed partition — it is CSV-only. Those mapping paths (measures → metrics, DAX dialect, `primary_key`, `source` resolution from `Sql.Database`) are covered by the synthetic `tests/fixtures/Sales.SemanticModel` only — written by hand, not exported by Power BI Desktop. Check a real export with these constructs before trusting them fully, and extend the fixture when a new construct turns up.

## Licensing

The repository is **PolyForm Noncommercial 1.0.0** (`LICENSE`, licensor Nils Becker), except the Apache-2.0 parts listed in `NOTICE`: `vendor/` (unmodified third-party copies) and the derived works `src/ossie_databricks/` and `tests/test_ossie_databricks.py`. Consequences when changing things:

- New own code needs nothing — the PolyForm license covers it.
- Anything ported or copied from Apache Ossie code keeps its ASF header plus a prominent note of what changed (Apache-2.0 §4b), and gets listed in `NOTICE`. It cannot be relicensed under the noncommercial terms.
- `pyproject.toml` declares `license = "PolyForm-Noncommercial-1.0.0 AND Apache-2.0"` with `LICENSE`, `NOTICE` and `LICENSES/*` as license files; keep them in sync with `NOTICE`.
- Never commit personal or machine data: check new sample models for local paths (`File.Contents(...)`), and keep `.pbix`/`.pbi/` state out.

## Commands

Python 3.13, [uv](https://docs.astral.sh/uv/) with the `uv_build` backend and a `src/` layout.

```bash
uv sync                 # create/refresh .venv from pyproject.toml
uv add <pkg>            # add a dependency (don't hand-edit pyproject deps)
uv run tmdl2ossie       # start the Streamlit UI (tmdl2ossie:main)
uv run python -m ...    # run anything inside the project venv
```

`uv run pytest` runs the suite, `uv run pytest tests/test_ui.py::test_name` a single test. `uv run ruff check` lints (config in `pyproject.toml`, `vendor/` excluded; there is no `ruff format` step, so the code is not auto-formatted). CI (`.github/workflows/ci.yml`) runs `uv sync --locked`, ruff, our tests and the vendored Ossie and Snowflake suites on every push to `main`. `uv run tmdl2ossie` starts the UI (extra args go to `streamlit run`, e.g. `--server.port 8600`). `uv run pytest tests/test_ossie_databricks.py` runs the ported Databricks tests alone. The vendored Snowflake converter's suite: `uv run pytest vendor/ossie-converters-main/snowflake/tests -q -p no:cacheprovider` (194 tests). The vendored upstream Ossie suite runs with `uv run pytest vendor/ossie/python_implementation/tests -q -p no:cacheprovider` (108 tests) — worth running after pulling an upstream update.

`[tool.uv.build-backend] module-name` lists `tmdl2ossie`, `tmdl_parser` and `ossie_databricks`. Without it the backend ships only the module matching the project name, so a new top-level package under `src/` must be added there or it silently stays out of the wheel (an editable dev install hides this).

## Ossie output side

`vendor/ossie/` is a verbatim copy of the Apache Ossie repo (`core-spec/`, `examples/`, `validation/`, `python_implementation/`) — keep it byte-identical so it stays swappable. It lives outside `src/` on purpose: anything under `src/` becomes an importable top-level name, and `ossie` would collide with the real package inside it. `apache-ossie` is not on PyPI, so vendoring is the only option; `uv add` wired it up as a workspace member, which is why it shares our venv and lock.

`vendor/ossie-converters-main/` is a verbatim copy of the Apache Ossie converters repo, kept byte-identical the same way. `snowflake/`, `cube/` and `honeydew/` are wired in as uv workspace members (`apache-ossie-snowflake` / `ossie_snowflake`, `apache-ossie-cube` / `ossie_cube`, `apache-ossie-honeydew` / `ossie_honeydew`); they need only PyYAML (and sqlglot). The repo's `ossie` git-submodule folder is empty: the converters do not need it, but 3 of the vendored Cube tests read the spec from it and fail here (with the folder symlinked to `vendor/ossie` in a temp copy, all 1189 pass). `dbt/` cannot be a workspace member as-is — its `tool.uv.sources` points `apache-ossie` at `../ossie/python`, which uv rejects. Wire further converters the same way: add the directory to `[tool.uv.workspace] members`, then `uv add <package-name>`.

Three facts that decide how to use it, all verified against the spec:

- **The JSON Schema is normative; the pydantic models are weaker.** `ossie.OssieDocument` accepts unknown properties in nested objects (and *silently drops* them), and treats `version` as neither required nor value-checked, where `ossie-schema.json` rejects all three. So validate generated output against the schema — passing pydantic validation proves less.
- **`src/tmdl2ossie/validation.py` is the facade to use** (`validate_model` / `validate_document` / `validate_file`). Upstream's `validation/validate.py` keeps its aggregation inside a CLI `main()` that prints and exits; the facade calls the five check functions itself and returns a `ValidationResult` with errors and warnings separated. Beyond the schema it adds unique names, relationship reference integrity, column arity and SQL syntax (sqlglot).
- **DAX is never syntax-checked.** sqlglot cannot parse it, so upstream skips the dialect entirely — a malformed DAX expression passes validation silently. Only portable SQL dialects get real scrutiny.

Division of labour for pack 3: build documents with the pydantic models (typed, correct aliases such as `from_dataset` → `from`, omits empty values), then validate the serialized result with the facade. Put Power BI metadata in `custom_extensions` (`vendor_name` is free-form; `POWER_BI` is a spec example) — anything in self-invented keys is dropped without an error.

## TMDL input format

The reference model lives in `samples/Retail/pbi_file.SemanticModel/definition/` (sample exported from `samples/Retail/pbi_file.pbip`; CSV sources sit in `samples/Retail/`). Its M source paths were anonymized to `C:\Daten\Retail\…` before publishing — the original pointed into a personal OneDrive. `.pbix`, `.pbi/cache.abf` and `.pbi/localSettings.json` are gitignored (data model with source paths, machine-bound signature). `samples/Retail.zip` is built from exactly the files git tracks under `samples/Retail/` (archive paths start at `Retail/`); rebuild it the same way if the sample changes, since `tests/test_ui.py` and the upload tab use it. Layout:

- `model.tmdl` — root. Carries model-level properties (`culture`, `defaultPowerBIDataSourceVersion`, …) and the table inventory as `ref table <name>` lines plus `ref cultureInfo <name>`. Start parsing here to learn which table files to read.
- `tables/<name>.tmdl` — one file per table; columns, measures, partitions, hierarchies.
- `relationships.tmdl` — all relationships, each `relationship <guid>` with `fromColumn: Table.Column` / `toColumn: Table.Column`.
- `database.tmdl`, `cultures/<locale>.tmdl` — compatibility level and locale metadata; low value for the conversion.

Syntax the parser has to handle:

- Tab-indented block structure. A block opens with `<keyword> <name>` (`table kunden`, `column Region`, `partition kunden = m`, `relationship <guid>`) and its properties are indented one level deeper.
- `property: value` lines, plus bare-word boolean flags on their own line (`isHidden`, `isPrivate`, `isDefault`, `isNameInferred`).
- `/// text` triple-slash lines *precede* the object they describe and become its description — these carry the business documentation, so they matter most for the Ossie output.
- `annotation Key = Value` lines, interleaved between blocks at every level; values can be JSON-ish arrays (`PBI_QueryOrder = ["kunden","produkte"]`).
- Multi-line expressions: `source =` followed by an indented M (Power Query) block, and calculated columns/measures written as `column Jahr = YEAR([Date])` with the DAX inline or indented below. Termination is by indentation, not a delimiter.
- Nested sub-blocks inside columns, e.g. `variation Variation` referencing a relationship and a default hierarchy.

Gotchas when deciding what to emit:

- `DateTableTemplate_*` and `LocalDateTable_*` tables are Power BI auto-generated time-intelligence artifacts (`isHidden` + `isPrivate`), not part of the user's semantic model, and should normally be filtered out. Filter *relationships* by whether an endpoint is such a table — **not** by the `AutoDetected_` name prefix, which only means Power BI inferred the relationship rather than the author drawing it. In the Retail sample both genuine joins carry that prefix, so filtering on it would leave no relationships at all.
- The sample model is German (`de-DE`): table names, column names and descriptions are German, and M expressions use German step names (`#"Höher gestufte Header"`) and absolute Windows source paths. Don't assume ASCII identifiers or portable paths.
- `lineageTag` GUIDs are the stable identity of tables/columns across edits; relationships reference columns by `Table.Column` name, not by tag.
