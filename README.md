# PBI Semantik

Convert a Power BI semantic model into the vendor-neutral
[Apache Ossie](https://github.com/apache/ossie) format, review and edit it in a
small web UI, and export it to other semantic layers.

```
Power BI project (.pbip, TMDL)  →  Apache Ossie  →  Snowflake · Databricks · Cube · Honeydew
```

Everything runs locally and offline. No Power BI, Fabric or warehouse
connection is needed.

## Features

The Streamlit UI has four tabs:

- **Upload:** a ZIP of a Power BI project, or a bundled sample model. The model is parsed and converted right away.
- **Edit:** descriptions, data types and dimension flags in forms, metrics (edit, add, delete) and the full Ossie YAML. Every change is validated against the Ossie JSON schema and the upstream semantic checks.
- **Graph:** an entity-relationship diagram with fact and dimension tables, keys and joins.
- **Export:** download the result for one of these targets:

| Target | Output | Converter |
|---|---|---|
| Ossie YAML / JSON | the Ossie document itself | — |
| Snowflake | Cortex Analyst semantic model (YAML) | vendored Apache Ossie converter |
| Databricks | Unity Catalog Metric View (YAML) | `ossie_databricks`, a Python port of the Apache Ossie Java converter |
| Cube | `model/cubes/*.yml` and `model/views/*.yml` as a ZIP | vendored Apache Ossie converter |
| Honeydew | workspace (`workspace.yml`, `schema/…`) as a ZIP | vendored Apache Ossie converter |

## Quick start

You need [uv](https://docs.astral.sh/uv/). It installs the right Python version
(3.13) itself.

```bash
git clone https://github.com/s6nlbeck/tmdl2ossie.git
cd tmdl2ossie
uv sync
uv run pbi-semantik          # opens the UI on http://localhost:8501
```

Run it from the repository checkout: the app reads the vendored Ossie
specification and the sample model from the repository at runtime.

Click **"Beispielmodell Retail laden"** to try it with the bundled sample (the
UI is in German).

## Preparing your own model

The tool reads the **TMDL** format, not `.pbix` files:

1. In Power BI Desktop, save the report as a project: *File → Save as → Power BI project (.pbip)*.
   Make sure the semantic model is stored as TMDL. Recent versions do this by default; older ones
   have it as a preview feature (*Store semantic model using TMDL format*).
2. Zip the project folder, or only its `*.SemanticModel` folder.
3. Upload the ZIP in the **Upload** tab.

Only the `.tmdl` files are extracted from the archive. Power BI's
auto-generated date tables (`LocalDateTable_*`, `DateTableTemplate_*`) are
left out.

## What to expect

- **DAX is not translated.** Measures and calculated columns are carried over as DAX
  expressions. Ossie keeps them, but SQL-based targets cannot use them, so they are
  skipped on export with a note. You can add SQL metrics in the **Edit** tab.
- **Sources.** Tables loaded from files have no database address. Snowflake, Databricks
  and the others need one, so the export form asks for database/catalog and schema.
- **Keys.** Power BI rarely marks primary keys. The mapping derives unique keys from
  relationships. Fact tables still lack one, which Cube needs; the export warns about it.
- **Notes and warnings.** Each export lists what could not be carried over. Anything that
  will break on the target platform is shown as a warning above the download.
- **Testing so far.** The pipeline has been tested against the bundled CSV-based sample
  model and the converters' own test suites, not yet against large production models.

The mapping rules (which TMDL construct becomes which Ossie element) are
documented in [`docs/tmdl-to-ossie-mapping.md`](docs/tmdl-to-ossie-mapping.md).

## How it works

| Path | Role |
|---|---|
| `src/tmdl_parser/` | Lossless TMDL parser (standard library only): grammar, vocabulary, dataclasses |
| `src/pbi_semantik/mapping.py` | TMDL → Ossie mapping, reporting anything it cannot express |
| `src/pbi_semantik/validation.py` | Validation against the Ossie JSON schema and the upstream semantic checks |
| `src/pbi_semantik/exporters.py` | Export targets and the platform-specific fix-ups before each converter runs |
| `src/pbi_semantik/app.py` | Streamlit UI |
| `src/ossie_databricks/` | Python port of the Ossie → Databricks Metric View converter |
| `samples/Retail/` | Sample Power BI project (CSV-based) used by the UI and the tests |
| `vendor/ossie/`, `vendor/ossie-converters-main/` | Unmodified copies of the Apache Ossie repositories |

## Development

```bash
uv run pytest                                    # project tests
uv run pytest vendor/ossie/python_implementation/tests -q
uv run pytest vendor/ossie-converters-main/snowflake/tests -q -p no:cacheprovider
```

`CLAUDE.md` holds the detailed design notes.

## License

This project is **source-available, not open source**. It is licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE):

- **Allowed:** personal use, research and experimentation without a commercial purpose, and use
  by educational institutions, public research organizations, charities and government bodies.
- **Not allowed without a separate license:** any commercial use, including use inside a company.
  To request a commercial license, email mail(at)nils-becker.org.

The vendored Apache Ossie code and the components derived from it
(`src/ossie_databricks/`, `tests/test_ossie_databricks.py`) remain under the
[Apache License 2.0](LICENSES/Apache-2.0.txt). See [`NOTICE`](NOTICE) for details.

Power BI is a trademark of Microsoft Corporation; Apache and Apache Ossie are
trademarks of The Apache Software Foundation. This project is not affiliated
with either.
