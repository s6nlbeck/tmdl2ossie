"""Tests for the UI layer: ZIP loading, the graph, the exporters and the Streamlit app itself."""

from __future__ import annotations

import copy
import io
import zipfile
from pathlib import Path

import pytest
import yaml
from ossie import OssieDocument
from streamlit.testing.v1 import AppTest

from tmdl2ossie.exporters import TARGETS, ExportError, _qualify_sources, _snowflake_identifier
from tmdl2ossie.graph import to_dot
from tmdl2ossie.loading import UploadError, load_archive, load_directory
from tmdl2ossie.validation import validate_document

ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_MODEL = ROOT / "samples" / "Retail" / "pbi_file.SemanticModel"
APP = ROOT / "src" / "tmdl2ossie" / "app.py"


def _zip(files: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _model_files(prefix: str) -> dict[str, bytes]:
    """The Retail definition folder, re-rooted under *prefix*."""
    definition = SEMANTIC_MODEL / "definition"
    return {
        f"{prefix}{file.relative_to(definition).as_posix()}": file.read_bytes()
        for file in definition.rglob("*")
        if file.is_file()
    }


@pytest.fixture(scope="module")
def retail() -> dict:
    return load_directory(SEMANTIC_MODEL).document


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_retail_zip_from_repo():
    [model] = load_archive((ROOT / "samples" / "Retail.zip").read_bytes(), fallback_name="Retail")
    assert model.label == "Retail/pbi_file.SemanticModel"
    assert model.document["name"] == "pbi_file"
    assert {dataset["name"] for dataset in model.document["datasets"]} == {"kunden", "produkte", "verkauefe"}
    assert validate_document(model.document).ok


def test_bare_definition_folder_uses_fallback_name():
    [model] = load_archive(_zip(_model_files("")), fallback_name="Mein Modell")
    assert model.document["name"] == "Mein Modell"


def test_several_models_in_one_archive():
    files = _model_files("a/A.SemanticModel/definition/") | _model_files("b/B.SemanticModel/definition/")
    models = load_archive(_zip(files), fallback_name="x")
    assert [model.label for model in models] == ["a/A.SemanticModel", "b/B.SemanticModel"]
    assert [model.document["name"] for model in models] == ["A", "B"]


def test_macos_resource_forks_are_ignored():
    files = _model_files("M.SemanticModel/definition/")
    files["__MACOSX/M.SemanticModel/definition/._model.tmdl"] = b"\x00\x05\x16\x07binary"
    files["M.SemanticModel/definition/tables/._kunden.tmdl"] = b"\x00\x05\x16\x07binary"
    [model] = load_archive(_zip(files), fallback_name="x")
    assert model.document["name"] == "M"


@pytest.mark.parametrize("name", ["../evil.tmdl", "/abs/evil.tmdl", "C:/evil.tmdl", "a/../../evil.tmdl"])
def test_path_traversal_is_rejected(name):
    with pytest.raises(UploadError, match="unzulässiger Pfad"):
        load_archive(_zip({name: "model Model"}), fallback_name="x")


def test_archive_without_model():
    with pytest.raises(UploadError, match="model.tmdl"):
        load_archive(_zip({"readme.txt": "nothing here"}), fallback_name="x")


def test_not_a_zip():
    with pytest.raises(UploadError, match="ZIP"):
        load_archive(b"definitely not a zip", fallback_name="x")


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #


def test_graph_roles_and_edges(retail):
    dot = to_dot(retail)
    assert dot.startswith("digraph ossie {") and dot.endswith("}")
    # verkauefe points at both other tables: a fact; they are dimensions.
    assert "verkauefe</B></FONT><FONT COLOR=\"#ffffff\" POINT-SIZE=\"9\">   Fakt" in dot
    assert dot.count("   Dimension</FONT>") == 2
    assert dot.count(" -> ") == len(retail["relationships"])
    # Edges attach to the field rows, not just the tables.
    assert '"ds_verkauefe":f2r:e -> "ds_kunden":f0l:w' in dot


def test_graph_without_fields(retail):
    dot = to_dot(retail, show_fields=False)
    assert "PORT=" not in dot
    assert " Felder</FONT>" in dot


def test_graph_tolerates_broken_documents(retail):
    broken = {
        **retail,
        "relationships": [{"name": "dangling", "from": "verkauefe", "to": "gibt_es_nicht"}, "garbage"],
        "metrics": [
            {"name": "Umsatz", "expression": {"dialects": []}},
            {"name": 'Quote "<&>"', "custom_extensions": [{"vendor_name": "POWER_BI", "data": "{not json"}]},
        ],
    }
    dot = to_dot(broken)
    assert " -> " not in dot
    assert "Metriken" in dot
    assert "Quote &quot;&lt;&amp;&gt;&quot;" in dot


def test_metrics_attach_to_home_table(retail):
    metric = {
        "name": "Gesamtmenge",
        "expression": {"dialects": [{"dialect": "DAX", "expression": "SUM(verkauefe[Menge])"}]},
        "custom_extensions": [{"vendor_name": "POWER_BI", "data": '{"table": "verkauefe"}'}],
    }
    dot = to_dot({**retail, "metrics": [metric]})
    assert "Gesamtmenge" in dot and "ohne Tabelle" not in dot
    assert "Gesamtmenge" not in to_dot({**retail, "metrics": [metric]}, show_metrics=False)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["ossie_yaml", "ossie_json"])
def test_ossie_exports_round_trip(retail, key, tmp_path):
    from tmdl2ossie.validation import validate_file

    artifact = TARGETS[key].export(OssieDocument.model_validate(retail), {})
    path = tmp_path / artifact.file_name
    path.write_text(artifact.content, encoding="utf-8")
    assert artifact.file_name.startswith("pbi_file.ossie.")
    assert validate_file(path).ok


def test_export_targets():
    assert list(TARGETS) == ["ossie_yaml", "ossie_json", "snowflake", "databricks", "cube", "honeydew"]


def _cube(document: dict, **options: str):
    return _zipped("cube", document, **options)


def _zipped(target: str, document: dict, **options: str):
    artifact = TARGETS[target].export(OssieDocument.model_validate(document), options)
    with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
        files = {name: archive.read(name).decode() for name in archive.namelist()}
    return artifact, files


def test_cube_export(retail):
    artifact, files = _cube(retail)
    assert artifact.file_name == "pbi_file.cube.zip"
    assert sorted(files) == [
        "model/cubes/kunden.yml", "model/cubes/produkte.yml", "model/cubes/verkauefe.yml", "model/views/pbi_file.yml",
    ]
    fact = yaml.safe_load(files["model/cubes/verkauefe.yml"])["cubes"][0]
    assert [join["name"] for join in fact["joins"]] == ["kunden", "produkte"]
    # Power BI metadata -- M code with local file paths -- must not leak into the Cube repo.
    assert not any("POWER_BI" in content or "Csv.Document" in content for content in files.values())
    # The fact table has no key, which Cube needs on a cube with joins: a warning, not a note.
    assert len(artifact.warnings) == 1 and "verkauefe" in artifact.warnings[0]
    assert "model/views/pbi_file.yml" in artifact.preview


def test_cube_export_with_key_and_schema(retail):
    document = copy.deepcopy(retail)
    next(d for d in document["datasets"] if d["name"] == "verkauefe")["primary_key"] = ["VerkaufsID"]
    artifact, files = _cube(document, schema="retail", database="analytics", dialect="SNOWFLAKE")
    assert artifact.warnings == []
    assert yaml.safe_load(files["model/cubes/kunden.yml"])["cubes"][0]["sql_table"] == "analytics.retail.kunden"


def _snowflake(document: dict, **options: str):
    return TARGETS["snowflake"].export(OssieDocument.model_validate(document), options)


def test_snowflake_needs_qualified_sources(retail):
    # Retail is CSV-backed, so its sources are bare table names.
    with pytest.raises(ExportError, match="Datenbank und Schema"):
        _snowflake(retail)


def test_snowflake_export(retail):
    artifact = _snowflake(retail, database="analytics", schema="retail")
    assert artifact.file_name == "pbi_file.snowflake.yaml"
    model = yaml.safe_load(artifact.content)
    tables = {table["name"]: table for table in model["tables"]}
    assert tables["kunden"]["base_table"] == {"database": "ANALYTICS", "schema": "RETAIL", "table": "KUNDEN"}
    assert [dimension["name"] for dimension in tables["verkauefe"]["time_dimensions"]] == ["Datum"]
    assert {relationship["right_table"] for relationship in model["relationships"]} == {"kunden", "produkte"}
    # One summary note instead of one converter warning per object.
    assert sum("custom_extensions" in note for note in artifact.notes) == 1
    assert any("`kunden` → `analytics.retail.kunden`" in note for note in artifact.notes)


def test_snowflake_skips_dax_metrics(retail):
    metric = {"name": "Gesamtmenge", "expression": {"dialects": [{"dialect": "DAX", "expression": "SUM(x[y])"}]}}
    artifact = _snowflake({**retail, "metrics": [metric]}, database="DB", schema="S")
    assert "metrics" not in yaml.safe_load(artifact.content)
    assert any("Gesamtmenge" in note and "DAX" in note for note in artifact.notes)


def test_source_qualification():
    data = {"datasets": [
        {"source": "kunden"},
        {"source": "dbo.Sales"},
        {"source": "db.dbo.Sales"},
        {"source": "SELECT * FROM t"},
        {"source": "Verkäufe"},
    ]}
    changes = _qualify_sources(data, "DB", "sch ema", _snowflake_identifier)
    assert [dataset["source"] for dataset in data["datasets"]] == [
        'DB."sch ema".kunden', "DB.dbo.Sales", "db.dbo.Sales", "SELECT * FROM t", 'DB."sch ema"."Verkäufe"',
    ]
    assert len(changes) == 3
    # Without a schema, a bare name cannot be completed and stays for the converter to reject.
    data = {"datasets": [{"source": "kunden"}]}
    assert _qualify_sources(data, "DB", None, _snowflake_identifier) == [] and data["datasets"][0]["source"] == "kunden"
    assert _snowflake_identifier('"Mixed"') == '"Mixed"' and _snowflake_identifier('a"b') == '"a""b"'


def _databricks(document: dict, **options: str):
    return TARGETS["databricks"].export(OssieDocument.model_validate(document), options)


def test_databricks_needs_qualified_sources(retail):
    with pytest.raises(ExportError, match="Katalog und Schema"):
        _databricks(retail)


def test_databricks_export(retail):
    artifact = _databricks(retail, catalog="main", schema="retail")
    assert artifact.file_name == "pbi_file.metric_view.yaml"
    view = yaml.safe_load(artifact.content)
    assert view["source"] == "main.retail.verkauefe"
    joins = {join["name"]: join for join in view["joins"]}
    assert joins["kunden"]["using"] == ["KundenID"]
    # The relationship's target key, derived by our mapping, lets Databricks rely on it.
    assert joins["kunden"]["rely"] == {"at_most_one_match": True}
    dimensions = {d["name"]: d["expr"] for d in view["dimensions"]}
    # The dimension-side key would collide with the fact's foreign key and is renamed.
    assert dimensions["KundenID"] == "KundenID"
    assert dimensions["kunden_KundenID"] == "kunden.KundenID"
    assert any("`kunden.KundenID` → `kunden_KundenID`" in note for note in artifact.notes)
    # Repeated per-field notices are folded into one line.
    assert sum("datatype has no Metric View counterpart" in note for note in artifact.notes) == 1


def test_databricks_drops_tables_without_relationship(retail):
    measure_table = {"name": "Kennzahlen", "source": "main.retail.kennzahlen", "fields": [
        {"name": "Platzhalter", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "x"}]}},
    ]}
    dialects = [{"dialect": "ANSI_SQL", "expression": "SUM(Umsatz)"}]
    metric = {"name": "Gesamtumsatz", "expression": {"dialects": dialects}}
    document = {**retail, "datasets": [*retail["datasets"], measure_table], "metrics": [metric]}
    artifact = _databricks(document, catalog="main", schema="retail")
    view = yaml.safe_load(artifact.content)
    # Without the fix-up the isolated table would be a second candidate fact.
    assert view["source"] == "main.retail.verkauefe"
    assert "Kennzahlen" not in artifact.content
    assert view["measures"] == [{"name": "Gesamtumsatz", "expr": "SUM(Umsatz)"}]
    assert any("`Kennzahlen`" in note for note in artifact.notes)


def test_databricks_explicit_fact(retail):
    view = yaml.safe_load(_databricks(retail, catalog="main", schema="retail", fact="verkauefe").content)
    assert view["source"] == "main.retail.verkauefe"
    # kunden as grain would put verkauefe one-to-many below it and produkte many-to-one
    # below that -- a mixed branch, which Databricks refuses.
    with pytest.raises(ExportError, match="same cardinality"):
        _databricks(retail, catalog="main", schema="retail", fact="kunden")


def _sql_metric(name: str, expression: str, dialect: str = "ANSI_SQL") -> dict:
    return {"name": name, "expression": {"dialects": [{"dialect": dialect, "expression": expression}]}}


def test_honeydew_export(retail):
    artifact, files = _zipped("honeydew", retail, database="ANALYTICS", schema="RETAIL")
    assert artifact.file_name == "pbi_file.honeydew.zip"
    assert "workspace.yml" in files and "schema/verkauefe/datasets/verkauefe.yml" in files
    entity = yaml.safe_load(files["schema/verkauefe/verkauefe.yml"])
    assert [relation["target_entity"] for relation in entity["relations"]] == ["kunden", "produkte"]
    assert yaml.safe_load(files["schema/kunden/datasets/kunden.yml"])["sql"] == "ANALYTICS.RETAIL.kunden"
    assert not any("POWER_BI" in content or "Csv.Document" in content for content in files.values())
    assert artifact.warnings == []


def test_honeydew_drops_dax(retail):
    document = copy.deepcopy(retail)
    fact = next(d for d in document["datasets"] if d["name"] == "verkauefe")
    fact["fields"].append({"name": "Marge", "expression": {"dialects": [
        {"dialect": "DAX", "expression": "verkauefe[Umsatz] - verkauefe[Kosten]"}]}})
    document["metrics"] = [
        _sql_metric("dax_umsatz", "SUM(verkauefe[Umsatz])", "DAX"),
        # A SQL alternative next to DAX survives; only the DAX entry goes.
        {"name": "umsatz", "expression": {"dialects": [
            {"dialect": "DAX", "expression": "SUM(verkauefe[Umsatz])"},
            {"dialect": "SNOWFLAKE", "expression": "SUM(verkauefe.Umsatz)"}]}},
    ]
    artifact, files = _zipped("honeydew", document)
    assert "schema/verkauefe/metrics/umsatz.yml" in files
    assert yaml.safe_load(files["schema/verkauefe/metrics/umsatz.yml"])["sql"] == "SUM(verkauefe.Umsatz)"
    assert not any("[Umsatz]" in content for content in files.values())
    assert any("`verkauefe.Marge`" in note and "`dax_umsatz`" in note for note in artifact.notes)


def test_honeydew_warns_about_unqualified_metric(retail):
    artifact, files = _zipped("honeydew", {**retail, "metrics": [_sql_metric("umsatz", "SUM(Umsatz)")]})
    assert len(artifact.warnings) == 1 and "verkauefe.Umsatz" in artifact.warnings[0]


def test_zip_paths_cannot_escape(retail):
    document = copy.deepcopy(retail)
    document["metrics"] = [_sql_metric("../../evil", "SUM(verkauefe.Umsatz)")]
    with pytest.raises(ExportError, match="Unzulässiger Dateipfad"):
        _zipped("honeydew", document)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


def _app() -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=30).run()


def test_app_starts_empty():
    app = _app()
    assert not app.exception
    assert app.session_state["doc"] is None


def test_app_loads_sample_and_renders_all_tabs():
    app = _app()
    next(button for button in app.button if "Retail" in button.label).click().run()
    assert not app.exception
    assert app.session_state["doc"]["name"] == "pbi_file"
    assert any("Ossie-konform" in item.value for item in app.sidebar.success)
    assert [metric.value for metric in app.metric] == ["3", "15", "2", "0"]
    # Export tab: the default target is Ossie YAML, which yields a download.
    assert app.radio[0].value == "ossie_yaml"


def test_app_yaml_edit_is_validated():
    app = _app()
    next(button for button in app.button if "Retail" in button.label).click().run()

    yaml_area = next(area for area in app.text_area if area.key and area.key.startswith("yaml_"))
    # A dangling relationship passes the structural check but fails validation.
    yaml_area.set_value(yaml_area.value.replace("to: kunden", "to: gibt_es_nicht"))
    next(button for button in app.button if button.label == "YAML übernehmen").click().run()
    assert not app.exception
    assert any("Validierungsfehler" in item.value for item in app.sidebar.error)

    # Platform export is blocked while invalid; reset brings the original back.
    assert not app.radio
    next(button for button in app.button if "verwerfen" in button.label).click().run()
    assert any("Ossie-konform" in item.value for item in app.sidebar.success)


def test_app_rejects_malformed_yaml():
    app = _app()
    next(button for button in app.button if "Retail" in button.label).click().run()
    before = app.session_state["doc"]
    yaml_area = next(area for area in app.text_area if area.key and area.key.startswith("yaml_"))
    yaml_area.set_value("name: x\nname: y\n").run()
    next(button for button in app.button if button.label == "YAML übernehmen").click().run()
    assert app.error
    assert app.session_state["doc"] == before


def test_app_model_form_updates_document():
    app = _app()
    next(button for button in app.button if "Retail" in button.label).click().run()
    app.text_input[0].set_value("Retail Verkauf")
    next(button for button in app.button if button.label == "Übernehmen").click().run()
    assert not app.exception
    assert app.session_state["doc"]["name"] == "Retail Verkauf"


def test_field_row_write_back():
    from tmdl2ossie.app import _apply_field_row, _fields_frame

    fields = [
        {"name": "Datum", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "Datum"}]},
         "dimension": {}, "datatype": "Date"},
        {"name": "Region", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "Region"}]},
         "dimension": {"is_time": False}, "datatype": "String", "label": "Geo"},
    ]
    rows = _fields_frame(fields).to_dict("records")
    assert [row["is_time"] for row in rows] == [True, False]

    # Unchanged rows leave the field exactly as written.
    for field, row in zip(fields, rows, strict=True):
        before = copy.deepcopy(field)
        _apply_field_row(field, row)
        assert field == before

    # Clearing a cell removes the property; un-ticking "Dimension" drops the block.
    _apply_field_row(fields[1], {**rows[1], "label": "", "description": float("nan"), "dimension": False})
    assert "label" not in fields[1] and "description" not in fields[1] and "dimension" not in fields[1]

    # Non-default is_time is spelled out, the datatype's default is not.
    _apply_field_row(fields[0], {**rows[0], "is_time": False})
    assert fields[0]["dimension"] == {"is_time": False}
    _apply_field_row(fields[0], {**rows[0], "is_time": True})
    assert fields[0]["dimension"] == {}


def test_app_snowflake_export():
    app = _app()
    next(button for button in app.button if "Retail" in button.label).click().run()
    app.radio[0].set_value("snowflake").run()
    assert any("Datenbank und Schema" in item.value for item in app.error)
    next(field for field in app.text_input if field.key == "export_snowflake_database").set_value("DB")
    next(field for field in app.text_input if field.key == "export_snowflake_schema").set_value("RETAIL").run()
    assert not app.exception and not app.error
    assert any("base_table" in code.value for code in app.code)


def test_app_databricks_export():
    app = _app()
    next(button for button in app.button if "Retail" in button.label).click().run()
    app.radio[0].set_value("databricks").run()
    assert any("Katalog und Schema" in item.value for item in app.error)
    assert next(box for box in app.selectbox if box.key == "export_databricks_fact").value == ""
    next(field for field in app.text_input if field.key == "export_databricks_catalog").set_value("main")
    next(field for field in app.text_input if field.key == "export_databricks_schema").set_value("retail").run()
    assert not app.exception and not app.error
    assert any("main.retail.verkauefe" in code.value for code in app.code)


def test_app_cube_export():
    app = _app()
    next(button for button in app.button if "Retail" in button.label).click().run()
    app.radio[0].set_value("cube").run()
    assert not app.exception and not app.error
    assert any("verkauefe" in item.value for item in app.warning)
    assert next(box for box in app.selectbox if box.key == "export_cube_dialect").value == ""
    assert any("model/cubes/kunden.yml" in code.value for code in app.code)


def test_app_honeydew_export():
    app = _app()
    next(button for button in app.button if "Retail" in button.label).click().run()
    app.radio[0].set_value("honeydew").run()
    assert not app.exception and not app.error
    assert any("workspace.yml" in code.value for code in app.code)
