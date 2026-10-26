"""Streamlit UI: upload a Power BI semantic model, edit the Ossie result, look at it, export it.

Start with ``uv run pbi-semantik`` (or ``uv run streamlit run src/pbi_semantik/app.py``).

All tabs work on one piece of state, ``st.session_state.doc``: the Ossie
document in its JSON form. It is kept as a plain dict rather than as an
``OssieDocument`` because the pydantic models silently drop unknown nested keys
-- a typo in the YAML editor would vanish instead of being reported by the
validator. The pydantic model is only built at export time, from a document that
has passed validation.

Every edit goes through :func:`_commit`, which bumps ``rev``. Widget keys carry
``rev``, so after a change every form is rebuilt from the new document instead
of holding on to stale input.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import yaml
from ossie import OssieDataType, OssieDialect, OssieDocument
from pydantic import ValidationError

from pbi_semantik.exporters import TARGETS, ExportError
from pbi_semantik.graph import METRIC_COLOR, ROLE_COLORS, to_dot
from pbi_semantik.loading import LoadedModel, UploadError, load_archive, load_directory
from pbi_semantik.mapping_helpers import TEMPORAL_TYPES
from pbi_semantik.validation import ValidationResult, load_yaml, validate_document

SAMPLE_MODEL = Path(__file__).resolve().parents[2] / "Retail" / "pbi_file.SemanticModel"
DATATYPES = [datatype.value for datatype in OssieDataType]
DIALECTS = [dialect.value for dialect in OssieDialect]
TEMPORAL = {datatype.value for datatype in TEMPORAL_TYPES}

ss = st.session_state


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def _init_state() -> None:
    defaults = {
        "doc": None,  # the Ossie document being edited (JSON form)
        "original": None,  # the document as converted, for "reset"
        "conversion_warnings": [],
        "source": "",
        "candidates": [],  # all models found in the last upload
        "upload_id": None,  # file_id of the upload already processed
        "upload_error": None,
        "rev": 0,
    }
    for key, value in defaults.items():
        ss.setdefault(key, value)


def _adopt(model: LoadedModel, source: str) -> None:
    """Make a freshly converted model the working document."""
    ss.original = copy.deepcopy(model.document)
    ss.conversion_warnings = model.warnings
    ss.source = f"{source} · {model.label}"
    _commit(model.document, message=f"{model.document['name']} geladen")


def _commit(document: dict[str, Any], *, message: str | None = None) -> None:
    ss.doc = document
    ss.rev += 1
    if message:
        ss.flash = message


def _working_copy() -> dict[str, Any]:
    return copy.deepcopy(ss.doc)


@st.cache_data(show_spinner=False, max_entries=32)
def _validate(document_json: str) -> ValidationResult:
    result = validate_document(json.loads(document_json))
    # Upstream warnings are a str subclass; plain strings keep the cache simple.
    return ValidationResult(errors=[str(e) for e in result.errors], warnings=[str(w) for w in result.warnings])


def _dump_yaml(document: dict[str, Any]) -> str:
    # Same settings as OssieDocument.to_ossie_yaml, applied to the raw dict.
    return yaml.dump(document, default_flow_style=False, sort_keys=False, allow_unicode=True)


# --------------------------------------------------------------------------- #
# Small helpers for editor values
# --------------------------------------------------------------------------- #


def _text(value: Any) -> str | None:
    """Normalize an editor cell: empty strings, None and pandas' NaN all mean "unset"."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    value = str(value).strip()
    return value or None


def _set(target: dict[str, Any], key: str, value: Any) -> None:
    """Set an optional property, removing it when empty -- the schema prefers absence."""
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


def _first_dialect(item: dict[str, Any]) -> dict[str, Any]:
    return item["expression"]["dialects"][0]


def _is_time(field: dict[str, Any]) -> bool:
    """The field's effective temporal role, mirroring ``OssieField.is_time_dimension``."""
    dimension = field.get("dimension")
    if dimension is None:
        return False
    if dimension.get("is_time") is not None:
        return bool(dimension["is_time"])
    return field.get("datatype") in TEMPORAL


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #


def run() -> None:
    st.set_page_config(page_title="Power BI → Ossie", page_icon=":material/hub:", layout="wide")
    _init_state()
    if message := ss.pop("flash", None):
        st.toast(message, icon=":material/check:")

    st.title("Power BI → Ossie")
    st.caption("Power-BI-Semantikmodell (TMDL) in das Apache-Ossie-Format überführen, prüfen und exportieren.")

    validation = _validate(json.dumps(ss.doc)) if ss.doc is not None else None
    _sidebar(validation)

    upload, edit, graph, export = st.tabs(
        [":material/upload: Upload", ":material/edit: Bearbeiten", ":material/hub: Graph", ":material/download: Export"]
    )
    with upload:
        _upload_tab()
    if ss.doc is None:
        for tab in (edit, graph, export):
            tab.info("Zuerst im Tab **Upload** ein Semantikmodell laden.", icon=":material/arrow_back:")
        return
    with edit:
        _edit_tab()
    with graph:
        _graph_tab()
    with export:
        _export_tab(validation)


def _sidebar(validation: ValidationResult | None) -> None:
    with st.sidebar:
        st.header("Status")
        if validation is None:
            st.info("Noch kein Modell geladen.")
            return
        st.markdown(f"**{ss.doc.get('name', '–')}**")
        st.caption(ss.source)

        if validation.ok:
            st.success("Ossie-konform", icon=":material/check_circle:")
        else:
            st.error(f"{len(validation.errors)} Validierungsfehler", icon=":material/error:")
            for error in validation.errors:
                st.markdown(f"- {error}")
        if validation.warnings:
            with st.expander(f"{len(validation.warnings)} Validierungshinweise"):
                for warning in validation.warnings:
                    st.markdown(f"- {warning}")
        if ss.conversion_warnings:
            with st.expander(f"{len(ss.conversion_warnings)} Konvertierungshinweise"):
                for warning in ss.conversion_warnings:
                    st.markdown(f"- {warning}")


# --------------------------------------------------------------------------- #
# Tab 1: Upload
# --------------------------------------------------------------------------- #


def _upload_tab() -> None:
    st.markdown(
        "ZIP-Archiv eines **PBIP-Projekts** oder eines `*.SemanticModel`-Ordners hochladen. "
        "Das Modell muss im **TMDL-Format** gespeichert sein (Power BI Desktop → *Speichern unter* → `.pbip`); "
        "eine `.pbix`-Datei allein reicht nicht."
    )
    uploaded = st.file_uploader("Semantikmodell (ZIP)", type=["zip"])

    # Convert once per upload; the uploader keeps returning the same file on
    # every rerun, and re-converting would discard the user's edits.
    if uploaded is not None and uploaded.file_id != ss.upload_id:
        ss.upload_id = uploaded.file_id
        try:
            with st.spinner("Lese und konvertiere TMDL …"):
                candidates = load_archive(uploaded.getvalue(), fallback_name=Path(uploaded.name).stem)
        except UploadError as error:
            ss.upload_error, ss.candidates = str(error), []
        else:
            ss.upload_error, ss.candidates = None, candidates
            _adopt(candidates[0], uploaded.name)
            st.rerun()

    if ss.upload_error:
        st.error(ss.upload_error, icon=":material/error:")

    if len(ss.candidates) > 1:
        labels = [candidate.label for candidate in ss.candidates]
        with st.container(border=True):
            st.markdown(f"Das Archiv enthält **{len(labels)} Semantikmodelle**.")
            choice = st.selectbox("Modell", range(len(labels)), format_func=labels.__getitem__)
            if st.button("Dieses Modell laden"):
                _adopt(ss.candidates[choice], uploaded.name if uploaded else "Upload")
                st.rerun()

    if SAMPLE_MODEL.is_dir() and st.button("Beispielmodell *Retail* laden", icon=":material/science:"):
        ss.candidates, ss.upload_error = [], None
        _adopt(load_directory(SAMPLE_MODEL), "Beispiel")
        st.rerun()

    if ss.doc is None:
        return

    st.divider()
    doc = ss.doc
    datasets = doc.get("datasets") or []
    columns = st.columns(4)
    columns[0].metric("Datasets", len(datasets))
    columns[1].metric("Felder", sum(len(dataset.get("fields") or []) for dataset in datasets))
    columns[2].metric("Beziehungen", len(doc.get("relationships") or []))
    columns[3].metric("Metriken", len(doc.get("metrics") or []))

    if ss.conversion_warnings:
        with st.expander(f"{len(ss.conversion_warnings)} Hinweise aus der Konvertierung", icon=":material/warning:"):
            for warning in ss.conversion_warnings:
                st.markdown(f"- {warning}")
    else:
        st.caption("Konvertierung ohne Hinweise – alle TMDL-Konstrukte wurden übernommen.")


# --------------------------------------------------------------------------- #
# Tab 2: Edit
# --------------------------------------------------------------------------- #


def _edit_tab() -> None:
    if ss.original is not None and ss.doc != ss.original:
        if st.button("Alle Änderungen verwerfen", icon=":material/undo:"):
            _commit(copy.deepcopy(ss.original), message="Auf das Konvertierungsergebnis zurückgesetzt")
            st.rerun()

    model, datasets, metrics, raw = st.tabs(["Modell", "Datasets & Felder", "Metriken", "YAML"])
    with model:
        _edit_model()
    with datasets:
        _edit_datasets()
    with metrics:
        _edit_metrics()
    with raw:
        _edit_yaml()


def _edit_model() -> None:
    doc = ss.doc
    with st.form(f"model_{ss.rev}"):
        name = st.text_input("Name", doc.get("name", ""))
        description = st.text_area("Beschreibung", doc.get("description") or "")
        if st.form_submit_button("Übernehmen", type="primary"):
            if not _text(name):
                st.error("Der Name darf nicht leer sein.")
                return
            updated = _working_copy()
            updated["name"] = _text(name)
            _set(updated, "description", _text(description))
            _commit(updated, message="Modell aktualisiert")
            st.rerun()


def _edit_datasets() -> None:
    datasets = ss.doc.get("datasets") or []
    names = [dataset["name"] for dataset in datasets]
    index = st.selectbox("Dataset", range(len(names)), format_func=names.__getitem__, key="edit_dataset")
    dataset = datasets[index]
    st.caption(
        "Namen sind hier schreibgeschützt, weil Beziehungen und Ausdrücke sie referenzieren – "
        "umbenennen im Tab **YAML**."
    )

    with st.form(f"dataset_{ss.rev}_{index}"):
        left, right = st.columns([2, 1])
        description = left.text_area("Beschreibung", dataset.get("description") or "", height=100)
        source = right.text_input(
            "Quelle (source)", dataset["source"], help="Physische Tabelle oder Abfrage, z. B. `db.schema.tabelle`."
        )
        fields = dataset.get("fields") or []
        edited = st.data_editor(
            _fields_frame(fields),
            hide_index=True,
            num_rows="fixed",
            disabled=["name", "dialect"],
            key=f"fields_{ss.rev}_{index}",
            column_config={
                "name": st.column_config.TextColumn("Feld", pinned=True),
                "datatype": st.column_config.SelectboxColumn("Datentyp", options=DATATYPES),
                "dimension": st.column_config.CheckboxColumn("Dimension", help="Gruppier-/Filterbares Attribut"),
                "is_time": st.column_config.CheckboxColumn("Zeit", help="Zeitdimension (nur wirksam als Dimension)"),
                "label": st.column_config.TextColumn("Label"),
                "description": st.column_config.TextColumn("Beschreibung", width="large"),
                "expression": st.column_config.TextColumn("Ausdruck"),
                "dialect": st.column_config.TextColumn("Dialekt"),
            },
        )
        if st.form_submit_button("Übernehmen", type="primary"):
            if not _text(source):
                st.error("Die Quelle darf nicht leer sein.")
                return
            updated = _working_copy()
            target = updated["datasets"][index]
            target["source"] = _text(source)
            _set(target, "description", _text(description))
            for field, row in zip(target.get("fields") or [], edited.to_dict("records"), strict=True):
                _apply_field_row(field, row)
            _commit(updated, message=f"Dataset {dataset['name']} aktualisiert")
            st.rerun()


def _fields_frame(fields: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for field in fields:
        dialects = field["expression"]["dialects"]
        rows.append(
            {
                "name": field["name"],
                "datatype": field.get("datatype"),
                "dimension": "dimension" in field,
                "is_time": _is_time(field),
                "label": field.get("label") or "",
                "description": field.get("description") or "",
                "expression": dialects[0]["expression"],
                # Only the first dialect is editable here; the rest live in the YAML.
                "dialect": dialects[0]["dialect"] + (f" (+{len(dialects) - 1})" if len(dialects) > 1 else ""),
            }
        )
    columns = ["name", "datatype", "dimension", "is_time", "label", "description", "expression", "dialect"]
    return pd.DataFrame(rows, columns=columns)


def _apply_field_row(field: dict[str, Any], row: dict[str, Any]) -> None:
    _set(field, "datatype", _text(row["datatype"]))
    _set(field, "label", _text(row["label"]))
    _set(field, "description", _text(row["description"]))
    if expression := _text(row["expression"]):
        _first_dialect(field)["expression"] = expression

    if not row["dimension"]:
        field.pop("dimension", None)
        return
    is_time = bool(row["is_time"])
    if "dimension" in field and _is_time(field) == is_time:
        return  # unchanged -- keep the dimension block exactly as it was written
    # Only spell out is_time when it differs from what the datatype implies.
    field["dimension"] = {} if is_time == (field.get("datatype") in TEMPORAL) else {"is_time": is_time}


def _edit_metrics() -> None:
    metrics = ss.doc.get("metrics") or []
    if metrics:
        names = [metric["name"] for metric in metrics]
        index = st.selectbox("Metrik", range(len(names)), format_func=names.__getitem__, key="edit_metric")
        metric = metrics[index]
        dialect = _first_dialect(metric)
        with st.form(f"metric_{ss.rev}_{index}"):
            left, right = st.columns(2)
            datatype = left.selectbox(
                "Datentyp", [None, *DATATYPES], index=_option_index([None, *DATATYPES], metric.get("datatype"))
            )
            dialect_name = right.selectbox("Dialekt", DIALECTS, index=_option_index(DIALECTS, dialect["dialect"]))
            expression = st.text_area("Ausdruck", dialect["expression"], height=160)
            description = st.text_area("Beschreibung", metric.get("description") or "", height=80)
            delete = st.checkbox("Metrik löschen")
            if st.form_submit_button("Übernehmen", type="primary"):
                updated = _working_copy()
                if delete:
                    del updated["metrics"][index]
                    if not updated["metrics"]:
                        del updated["metrics"]
                    _commit(updated, message=f"Metrik {metric['name']} gelöscht")
                    st.rerun()
                if not _text(expression):
                    st.error("Der Ausdruck darf nicht leer sein.")
                    return
                target = updated["metrics"][index]
                _set(target, "datatype", datatype)
                _set(target, "description", _text(description))
                _first_dialect(target).update(dialect=dialect_name, expression=expression.strip())
                _commit(updated, message=f"Metrik {metric['name']} aktualisiert")
                st.rerun()
    else:
        st.info("Das Modell enthält keine Metriken (Power-BI-Measures).")

    with st.expander("Neue Metrik anlegen", icon=":material/add:"):
        with st.form(f"new_metric_{ss.rev}"):
            left, right = st.columns(2)
            name = left.text_input("Name")
            dialect_name = right.selectbox("Dialekt", DIALECTS)
            expression = st.text_area("Ausdruck", placeholder="SUM(verkaeufe.Umsatz)", height=100)
            description = st.text_input("Beschreibung")
            if st.form_submit_button("Anlegen", type="primary"):
                name, expression = _text(name), _text(expression)
                if not name or not expression:
                    st.error("Name und Ausdruck sind Pflicht.")
                    return
                if name in {metric["name"] for metric in metrics}:
                    st.error(f"Eine Metrik {name!r} gibt es schon.")
                    return
                updated = _working_copy()
                metric = {"name": name, "expression": {"dialects": [{"dialect": dialect_name, "expression": expression}]}}
                _set(metric, "description", _text(description))
                updated.setdefault("metrics", []).append(metric)
                _commit(updated, message=f"Metrik {name} angelegt")
                st.rerun()


def _option_index(options: list[Any], value: Any) -> int:
    return options.index(value) if value in options else 0


def _edit_yaml() -> None:
    st.caption(
        "Vollzugriff auf das Ossie-Dokument – für alles, was die Formulare nicht abdecken "
        "(Umbenennen, Beziehungen, `ai_context`, `custom_extensions`, weitere Dialekte)."
    )
    text = st.text_area("Ossie-YAML", _dump_yaml(ss.doc), height=600, key=f"yaml_{ss.rev}", label_visibility="collapsed")
    if not st.button("YAML übernehmen", type="primary"):
        return
    try:
        data = load_yaml(text)
    except yaml.YAMLError as error:
        st.error(f"Ungültiges YAML:\n\n```\n{error}\n```")
        return
    if not isinstance(data, dict):
        st.error("Das Dokument muss ein YAML-Mapping sein.")
        return
    # Structural check only: the forms and the graph rely on the basic shape.
    # Everything finer (unknown keys, references, SQL) is the validator's job
    # and shows up in the sidebar, so a half-finished edit can still be kept.
    try:
        OssieDocument.model_validate(data)
    except ValidationError as error:
        st.error(f"Struktur passt nicht zum Ossie-Modell:\n\n```\n{error}\n```")
        return
    _commit(data, message="YAML übernommen")
    st.rerun()


# --------------------------------------------------------------------------- #
# Tab 3: Graph
# --------------------------------------------------------------------------- #


def _graph_tab() -> None:
    controls = st.columns([1, 1, 2])
    show_fields = controls[0].toggle("Felder anzeigen", value=True)
    show_metrics = controls[1].toggle("Metriken anzeigen", value=True)
    direction = controls[2].segmented_control(
        "Ausrichtung",
        ["LR", "TB"],
        default="LR",
        required=True,
        format_func={"LR": "Horizontal", "TB": "Vertikal"}.__getitem__,
        label_visibility="collapsed",
    )

    dot = to_dot(ss.doc, show_fields=show_fields, show_metrics=show_metrics, direction=direction)
    # Natural size: stretching would blow a three-table model up to full width.
    st.graphviz_chart(dot)

    swatch = '<span style="display:inline-block;width:.8em;height:.8em;border-radius:2px;background:{};margin:0 .3em 0 1em"></span>{}'
    legend = [swatch.format(color, role) for role, color in ROLE_COLORS.items()]
    legend.append(swatch.format(METRIC_COLOR, "ƒ Metrik"))
    st.markdown(
        "<small>" + "".join(legend) + "&nbsp;&nbsp;·&nbsp;&nbsp;<b>PK</b> Primärschlüssel, <b>UK</b> eindeutiger "
        "Schlüssel, <b>FK</b> Fremdschlüssel · Krähenfuß = n-Seite · Tooltips beim Überfahren</small>",
        unsafe_allow_html=True,
    )
    with st.expander("DOT-Quelltext"):
        st.code(dot, language="dot")


# --------------------------------------------------------------------------- #
# Tab 4: Export
# --------------------------------------------------------------------------- #


def _export_tab(validation: ValidationResult) -> None:
    if not validation.ok:
        st.error(
            "Das Dokument ist nicht Ossie-konform (Details in der Seitenleiste). "
            "Exportiert wird erst nach der Korrektur.",
            icon=":material/block:",
        )
        st.download_button(
            "Arbeitsstand als YAML sichern",
            _dump_yaml(ss.doc),
            file_name="arbeitsstand.ossie.yaml",
            mime="application/yaml",
            icon=":material/save:",
        )
        return

    key = st.radio("Zielplattform", list(TARGETS), format_func=lambda k: TARGETS[k].label, horizontal=True)
    target = TARGETS[key]
    st.caption(target.description)

    options = {}
    if target.options:
        columns = st.columns(len(target.options))
        dataset_names = ["", *(dataset["name"] for dataset in ss.doc.get("datasets") or [])]
        for column, option in zip(columns, target.options, strict=True):
            # Keyed per target and not by `rev`: the answers belong to the
            # platform, not to one version of the document.
            key = f"export_{target.key}_{option.key}"
            if option.kind == "choice":
                labels = dict(option.choices)
                value = column.selectbox(
                    option.label, list(labels), key=key, help=option.help, format_func=labels.__getitem__
                )
            elif option.kind == "dataset":
                if ss.get(key) not in dataset_names:
                    ss.pop(key, None)  # the dataset was renamed or removed meanwhile
                value = column.selectbox(
                    option.label, dataset_names, key=key, help=option.help, format_func=lambda v: v or "automatisch"
                )
            else:
                value = column.text_input(option.label, key=key, help=option.help, placeholder=option.placeholder)
            if value and value.strip():
                options[option.key] = value.strip()

    try:
        artifact = target.export(OssieDocument.model_validate(ss.doc), options)
    except NotImplementedError:
        function = f"{target.export.__module__}.{target.export.__name__}"
        st.info(
            f"Die Integration für **{target.label}** ist noch nicht angebunden. Einstiegspunkt: `{function}`.",
            icon=":material/construction:",
        )
        return
    except ExportError as error:
        st.error(f"Export nach {target.label} nicht möglich: {error}", icon=":material/error:")
        return

    for warning in artifact.warnings:
        st.warning(warning, icon=":material/warning:")
    st.download_button(
        f"{artifact.file_name} herunterladen",
        artifact.content,
        file_name=artifact.file_name,
        mime=artifact.mime,
        type="primary",
        icon=":material/download:",
    )
    if artifact.notes:
        with st.expander(f"{len(artifact.notes)} Hinweise zur Umwandlung", icon=":material/info:"):
            for note in artifact.notes:
                st.markdown(f"- {note}")
    preview = artifact.preview if artifact.preview is not None else artifact.content
    if isinstance(preview, str):
        language = {"yaml": "yaml", "json": "json", "sql": "sql", "zip": "yaml"}.get(artifact.file_name.rsplit(".", 1)[-1])
        with st.expander("Vorschau", expanded=True):
            st.code(preview, language=language, line_numbers=True, height=500)


if __name__ == "__main__":
    run()
