# TMDL → Ossie mapping

How a parsed Power BI semantic model (`tmdl_parser.datamodels`) is turned into an
Apache Ossie document (`ossie.models`). There is no intermediate application
format: the parser's dataclasses go straight into the Ossie pydantic models.

Sources: `vendor/ossie/core-spec/ossie-schema.json` (normative) and
`vendor/ossie/core-spec/spec.yaml` (annotated reference). Ossie version targeted:
`0.2.0.dev0`.

## Shape of the two sides

TMDL is **table-centric and physical**: tables own columns, measures, hierarchies
and the Power Query code that loads them. Ossie is **model-centric and logical**:
datasets own fields, while metrics and relationships live at the model root.

```
TMDLSemanticModel          →  OssieDocument
├── model (TMDLModel)      →      name, description, custom_extensions
├── database               →      custom_extensions
├── tables   [TMDLTable]   →  ├── datasets      [OssieDataset]
│   ├── columns            →  │   └── fields    [OssieField]
│   ├── measures           →  ├── metrics       [OssieMetric]   (lifted to root)
│   ├── hierarchies        →      (no counterpart — custom_extensions)
│   └── partitions         →      dataset.source + custom_extensions
├── relationships          →  └── relationships [OssieRelationship]
└── cultures               →      (dropped)
```

Three consequences worth internalising before reading the tables below:

- **Measures get lifted out of their table.** Ossie metrics are model-level, so
  the owning table is not expressible and has to be recorded as an extension.
- **Ossie has no hierarchies, no display formatting, no hidden flag.** Everything
  in that category either goes into `custom_extensions` or is dropped.
- **Ossie expects SQL.** TMDL carries M (Power Query) and DAX. Neither is SQL,
  and only DAX has a matching dialect in the enum; M has none at all.

## Document root

`TMDLSemanticModel` → `OssieDocument`

| Ossie | Source | Rule |
| --- | --- | --- |
| `version` | — | Constant `"0.2.0.dev0"`. The schema pins it with `const`. |
| `name` | `source_path` | **Not** `model.name` — TMDL always writes the literal `model Model`. Use the `.SemanticModel` directory stem (`pbi_file`), caller-overridable. |
| `description` | `model.description` | `/// ` lines above the `model` block. Usually absent. |
| `ai_context` | — | Not emitted initially. Candidate later source: `cultures/` linguistic metadata (synonyms). |
| `datasets` | `user_tables` | Required, `minItems: 1`. See below. |
| `relationships` | `relationships` | Filtered, see below. Omit the key entirely if empty. |
| `metrics` | all tables' `measures` | Omit the key entirely if empty. |
| `custom_extensions` | `model` + `database` | `culture`, `defaultPowerBIDataSourceVersion`, `sourceQueryCulture`, `valueFilterBehavior`, `dataAccessOptions`, model-level `annotations`, `database.compatibilityLevel`. |

## Datasets

`TMDLTable` → `OssieDataset`, for `TMDLSemanticModel.user_tables` only —
`DateTableTemplate_*` / `LocalDateTable_*` are Power BI's time-intelligence
artifacts, not author content.

| Ossie | Source | Rule |
| --- | --- | --- |
| `name` | `table.name` | Verbatim, including spaces and umlauts. Ossie only requires `minLength: 1`; see *Identifier hazards*. |
| `source` | `partitions[0]` | Required. See *Rule: dataset source*. |
| `description` | `table.description` | The `/// ` text. |
| `primary_key` | columns with `isKey` | Omit when no column is flagged. Power BI rarely sets this. |
| `unique_keys` | relationship targets | See *Rule: unique keys*. |
| `fields` | `table.columns` | All of them, including hidden ones. |
| `ai_context` | — | Not emitted initially. |
| `custom_extensions` | leftovers | `lineageTag`, `isHidden`, `isPrivate`, `excludeFromModelRefresh`, `annotations`, `hierarchies`, the full partition list. |

### Rule: dataset source

`source` is required and documented as `database.schema.table` **or a query**.
TMDL instead holds a partition whose `source` is M code. Two tiers:

1. **Recognisable native source** — the M matches a known relational pattern
   (`Sql.Database(...){[Schema=…,Item=…]}`, `Value.NativeQuery(...)`,
   `Snowflake.Databases(...)`, …): emit `database.schema.table`, or the native
   SQL string when the partition embeds a query.
2. **Anything else** (the Retail sample: `Csv.Document(File.Contents(...))`):
   emit the **table name** as a bare identifier. A CSV on a local disk has no
   portable physical address; inventing one would be a lie, and leaving the field
   out is not allowed.

Either way the complete M text goes into the dataset's `custom_extensions`, so no
information is lost and tier 1 can be improved later without touching tier 2.

Tables with multiple partitions: use the first for `source`, keep all of them in
the extension, and warn — Ossie cannot express partitioning.

### Rule: unique keys

Power BI stores cardinality on the relationship, not on the table. Ossie goes the
other way: it derives many-to-one vs. one-to-one from the target dataset's
`primary_key` / `unique_keys`. So the information must be moved across.

For every relationship kept, add its **to**-side column to the target dataset's
`unique_keys` as a single-column key, unless it is already the `primary_key`.
This is sound because Power BI enforces uniqueness on the one side of a
relationship. Without it, every relationship in the output is cardinality-less.

## Fields

`TMDLColumn` → `OssieField`

| Ossie | Source | Rule |
| --- | --- | --- |
| `name` | `column.name` | Verbatim. Relationship `*_columns` must match these. |
| `expression` | `sourceColumn` or `expression` | See *Rule: field expression*. Required. |
| `datatype` | `dataType` | See *Rule: data types*. |
| `dimension.is_time` | `dataType` + annotations | See *Rule: time dimensions*. |
| `label` | `displayFolder` | The spec's example value is `"filter"`, so this is a loose fit, but it is the only slot for a grouping label. Omit when unset. |
| `description` | `column.description` | The `/// ` text — the main business documentation in a TMDL file. |
| `ai_context` | — | Not emitted initially. |
| `custom_extensions` | leftovers | `lineageTag`, `formatString`, `summarizeBy`, `isHidden`, `isKey`, `isNullable`, `isUnique`, `sortByColumn`, `dataCategory`, `variations`, `annotations`. |

Hidden columns are **kept**. They are usually the key columns a relationship
points at; dropping them would produce a dangling reference that the validator
rejects. `isHidden` is preserved as an extension instead.

### Rule: field expression

- **Data column** (`sourceColumn` set): `dialect: ANSI_SQL`, `expression:
  <sourceColumn>` — a bare column reference, which is valid in every SQL dialect
  and is what the official examples do.
- **Calculated column** (`is_calculated`, DAX inline after `=`): `dialect: DAX`,
  `expression: <DAX>`.

Mixing dialects across fields of one dataset is allowed — `dialects` is a list
per field, and nothing requires all fields to share one.

⚠️ **DAX is never syntax-checked.** sqlglot cannot parse it, so the upstream
validator skips the dialect entirely; malformed DAX passes validation silently.

### Rule: data types

| TMDL `dataType` | Ossie `datatype` | Note |
| --- | --- | --- |
| `string` | `String` | |
| `int64` | `Integer` | |
| `double` | `Float` | Power BI "Decimal number" is binary floating point. |
| `decimal` | `Decimal` | Power BI "Fixed decimal number". |
| `boolean` | `Boolean` | |
| `dateTime` | `DateTime` | …unless overridden, next row. |
| `dateTime` + `annotation UnderlyingDateTimeDataType = Date` | `Date` | Power BI stores dates as `dateTime` and records the real type in an annotation. The Retail sample's `verkauefe.Datum` is exactly this case. |
| `dateTime` + `… = Time` | `Time` | Same mechanism. |
| `binary`, `variant` | `Opaque` | Plus a `custom_extensions` entry naming the original type. |
| `automatic`, `unknown`, unset | *(omit)* | `datatype` is optional; omitting is more honest than guessing. |

No TMDL type maps to `DateTimeTz` — Power BI's `dateTime` carries no offset.

### Rule: time dimensions

`dimension` is the only marker that makes a field usable for grouping, and
`is_time` is a *role*, independent of the datatype.

- Temporal datatype (`Date`/`Time`/`DateTime`): emit `dimension: {}` and let
  `is_time` default to true. Emit `is_time: false` explicitly only when the
  column is temporal but not meant for time-series use.
- `summarizeBy: none` on a non-temporal column: emit `dimension: {is_time:
  false}`. Power BI's "do not aggregate" is the closest available signal for
  "this is an attribute, not a measure".
- Numeric column with `summarizeBy: sum`/`average`/… : no `dimension` block.
  It is aggregable data; Ossie would call it metric input, not a dimension.

Foreign-key columns are numeric *and* `summarizeBy: none` and therefore land in
the second case — which matches the official TPC-DS example, where every `*_sk`
column carries an explicit `is_time: false`.

## Relationships

`TMDLRelationship` → `OssieRelationship`

**Which ones survive.** Filter on *endpoints*, not on the name:

- Drop a relationship if either endpoint table is an auto date table
  (`is_auto_date_table`). This removes the date-variation links.
- **Keep** `AutoDetected_*` relationships whose endpoints are both user tables.
  The prefix only means Power BI inferred it, not that it is synthetic — in the
  Retail sample both real joins (`verkauefe→kunden`, `verkauefe→produkte`) carry
  it. Filtering by prefix would empty the model.
- Drop `isActive: false` relationships and warn. Ossie has no inactive
  relationship; they only ever apply inside a DAX `USERELATIONSHIP`.

| Ossie | Source | Rule |
| --- | --- | --- |
| `name` | generated | See *Rule: relationship names*. |
| `from` | `fromColumn` table part | The **many** side. |
| `to` | `toColumn` table part | The **one** side. |
| `from_columns` | `fromColumn` column part | Always a single-element list. |
| `to_columns` | `toColumn` column part | Always a single-element list. |
| `ai_context` | — | Not emitted initially. |
| `custom_extensions` | leftovers | Original GUID `name`, `crossFilteringBehavior`, `securityFilteringBehavior`, `joinOnDateBehavior`, `relyOnReferentialIntegrity`, cardinalities. |

Split `Table.Column` with `helpers.split_column_reference`; it handles the quoted
and bracketed spellings (`'a b'.c`, `a[b]`).

**Direction.** Power BI writes the many side as `fromColumn` by default and omits
the cardinality properties, which is why the sample has none. When
`fromCardinality: one` and `toCardinality: many` are present, **swap** the two
ends — Ossie requires `from` to be the many side.

**Arity.** TMDL relationships are always single-column; composite keys are
modelled in Power BI by concatenating into a helper column. So the upstream
`validate_relationship_column_arity` check can never fail here.

### Rule: relationship names

TMDL names relationships by GUID. Ossie only needs uniqueness, but GUIDs are
unreadable in generated output, so emit `<from>_to_<to>` (e.g.
`verkauefe_to_kunden`). On collision — two relationships between the same pair of
tables — append the from-column: `verkauefe_to_kunden_KundenID`. The original
GUID stays in `custom_extensions`, since DAX measures may reference it.

## Metrics

`TMDLMeasure` → `OssieMetric`, collected from **all** user tables into one
model-level list.

| Ossie | Source | Rule |
| --- | --- | --- |
| `name` | `measure.name` | Measure names are unique model-wide in Power BI, so flattening cannot collide. |
| `expression` | `measure.expression` | `dialect: DAX`, expression verbatim including line breaks. Not syntax-checked. |
| `description` | `measure.description` | The `/// ` text. |
| `datatype` | `measure.dataType` | Same table as fields; usually unset on measures, then omit. |
| `ai_context` | — | Not emitted initially. |
| `custom_extensions` | leftovers | **Owning table name** (not otherwise expressible), `formatString`, `displayFolder`, `isHidden`, `lineageTag`. |

⚠️ The Retail sample has **no measures**. This path is covered by the hand-written
fixture `tests/fixtures/Sales.SemanticModel` only; check a real export with
measures before trusting it fully.

## Dropped and degraded

| TMDL | Fate |
| --- | --- |
| Hierarchies + levels | No Ossie counterpart. Into the dataset's `custom_extensions`. |
| Column variations | No counterpart; they only point at auto date tables, which are already filtered. Into the field's `custom_extensions`. |
| Partitions / M code | `source` keeps at most an address; the M text goes into `custom_extensions`. |
| `cultures/*.tmdl` | Dropped. Linguistic synonyms are a future `ai_context` source. |
| `formatString`, `summarizeBy`, `displayFolder`, `dataCategory`, `sortByColumn` | Display/aggregation semantics with no Ossie equivalent. `displayFolder` partially survives as `label`. |
| `lineageTag` | The stable identity across Power BI edits; worth keeping for round-tripping. Into `custom_extensions`. |
| `isHidden` / `isPrivate` | No visibility concept in Ossie. Into `custom_extensions`. |
| `annotations` | Into `custom_extensions` of the owning object. |
| Auto date tables + their relationships | Dropped entirely. |

### Custom extension format

Ossie's `custom_extensions` entries are `{vendor_name: str, data: str}` — `data`
is a **string**, not an object. Convention for this converter: one entry per
object, `vendor_name: "POWER_BI"`, `data` = compact JSON of the dropped
properties.

```yaml
custom_extensions:
  - vendor_name: POWER_BI
    data: '{"lineageTag":"9c6e...","formatString":"0","summarizeBy":"none"}'
```

`vendor_name` is a free-form string; `POWER_BI` is listed as an example in
`spec.yaml` but is absent from the `OssieVendor` enum — which does not matter,
because the field is typed `str`, not the enum.

⚠️ Anything written to a key the schema does not know is **silently dropped** by
the pydantic models (they are more permissive than the schema and discard
unknown nested properties without error). Extensions are the only sanctioned
place for non-Ossie data.

## Identifier hazards

Ossie imposes no character restrictions on names (`minLength: 1` only), so German
table and column names pass validation unchanged. Downstream converters that
generate SQL will not be as forgiving: `verkauefe` is fine, a name with spaces or
umlauts is not, and would need quoting the converter may not apply.

Decision: **keep original names**, do not transliterate. Renaming silently breaks
the link back to the Power BI model, and Ossie is the interchange format, not the
execution target. Revisit if a concrete downstream converter demands it.

## Validation

Every generated document goes through `tmdl2ossie.validation.validate_model`
before being written. It checks the normative JSON Schema plus unique names,
relationship reference integrity, column arity and SQL syntax. Note again that
DAX expressions are exempt from the SQL check, so a measure or calculated column
can be nonsense and still validate.

## Open questions

- **Document name** when converting several `.SemanticModel` directories at once
  — directory stem, or caller-supplied?
- **Native source detection** (tier 1 above) — which M connector patterns to
  support first. Needs a real model with a database source; the Retail sample is
  CSV-only.
- **`ai_context`** — the cultures file holds linguistic synonyms. Worth mining,
  or is the payoff too small for the parsing effort?
- **Calculated tables** (`partition x = calculated`, DAX instead of M) — the
  `source` rule has no tier for them yet. Not present in the sample.
