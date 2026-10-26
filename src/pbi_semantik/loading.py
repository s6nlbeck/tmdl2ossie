"""Turn an uploaded ZIP archive into converted Ossie documents.

The archive comes from a browser upload and is therefore untrusted. Only the
``.tmdl`` files are extracted -- the parser reads nothing else, and skipping the
rest keeps ``.pbix`` binaries, ``cache.abf`` and the report definition out of
the temp directory. Member paths are checked before anything is written, and the
total extracted size is capped.

An archive may hold several semantic models (a .pbip folder with more than one
``*.SemanticModel``), so loading returns one :class:`LoadedModel` per model and
leaves the choice to the caller.
"""

from __future__ import annotations

import io
import json
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pbi_semantik.mapping import MappingError, convert
from tmdl_parser import TMDLParseError, parse_semantic_model
from tmdl_parser.parser import MODEL_FILE

# TMDL is plain text; even large enterprise models stay well below this.
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 20_000
_CHUNK = 1024 * 1024


class UploadError(ValueError):
    """The archive cannot be read or holds no usable semantic model."""


@dataclass(frozen=True, slots=True)
class LoadedModel:
    """One converted semantic model, ready to be put into the UI's state."""

    #: Where the model sat inside the archive, e.g. ``Retail/pbi_file.SemanticModel``.
    label: str
    #: The Ossie document in its JSON form -- what the editor works on.
    document: dict[str, Any]
    #: Conversion warnings: TMDL constructs that could not be carried over.
    warnings: list[str]


def load_archive(data: bytes, *, fallback_name: str) -> list[LoadedModel]:
    """Extract, parse and convert every semantic model in a ZIP archive.

    *fallback_name* names the document when the archive holds a bare
    ``definition`` folder, whose surroundings carry no model name.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise UploadError(f"keine gültige ZIP-Datei: {error}") from None

    with archive, tempfile.TemporaryDirectory(prefix="pbi-semantik-") as tmp:
        root = Path(tmp)
        _extract_tmdl(archive, root)
        definitions = find_definitions(root)
        if not definitions:
            raise UploadError(
                f"keine {MODEL_FILE} im Archiv gefunden -- erwartet wird ein PBIP-Projekt "
                "oder ein *.SemanticModel-Ordner im TMDL-Format (eine .pbix-Datei reicht nicht)"
            )
        return [
            _load_definition(definition, label=_label(definition, root), fallback_name=fallback_name)
            for definition in definitions
        ]


def load_directory(path: str | Path) -> LoadedModel:
    """Convert a semantic model that is already on disk (used for the bundled sample)."""
    path = Path(path)
    return _load_definition(path, label=path.name, fallback_name=path.stem)


def find_definitions(root: Path) -> list[Path]:
    """Every directory below *root* that holds a ``model.tmdl``."""
    return sorted(file.parent for file in root.rglob(MODEL_FILE) if file.is_file())


def _load_definition(path: Path, *, label: str, fallback_name: str) -> LoadedModel:
    try:
        model = parse_semantic_model(path)
        # The document name normally comes from the `<name>.SemanticModel`
        # folder; a bare `definition` folder has no such parent.
        name = None if model.source_path.parent.name.endswith(".SemanticModel") else fallback_name
        result = convert(model, name=name)
    except (TMDLParseError, MappingError) as error:
        raise UploadError(f"{label}: {error}") from error

    document = json.loads(result.document.to_ossie_json())
    return LoadedModel(label=label, document=document, warnings=list(result.warnings))


def _label(definition: Path, root: Path) -> str:
    relative = definition.relative_to(root)
    # `.../pbi_file.SemanticModel/definition` reads better without the suffix.
    if relative.name == "definition" and relative.parent != Path("."):
        relative = relative.parent
    return relative.as_posix() or "."


def _extract_tmdl(archive: zipfile.ZipFile, target: Path) -> None:
    members = [info for info in archive.infolist() if _wanted(info)]
    if len(members) > MAX_MEMBERS:
        raise UploadError(f"zu viele TMDL-Dateien im Archiv ({len(members)})")

    budget = MAX_EXTRACTED_BYTES
    for info in members:
        destination = target.joinpath(*_safe_parts(info.filename))
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Count the bytes actually inflated: the sizes in the ZIP header are
        # whatever the archive claims and prove nothing.
        with archive.open(info) as source, destination.open("wb") as sink:
            while chunk := source.read(_CHUNK):
                budget -= len(chunk)
                if budget < 0:
                    raise UploadError(
                        f"entpackter Inhalt größer als {MAX_EXTRACTED_BYTES // (1024 * 1024)} MB"
                    )
                sink.write(chunk)


def _wanted(info: zipfile.ZipInfo) -> bool:
    if info.is_dir() or not info.filename.lower().endswith(".tmdl"):
        return False
    path = PurePosixPath(info.filename.replace("\\", "/"))
    # macOS Finder adds `__MACOSX/._model.tmdl` resource forks: binary junk
    # that carries the right extension.
    return "__MACOSX" not in path.parts and not path.name.startswith("._")


def _safe_parts(name: str) -> tuple[str, ...]:
    """Split a member name into path parts, rejecting anything that escapes the target."""
    path = PurePosixPath(name.replace("\\", "/"))
    parts = path.parts
    if path.is_absolute() or ".." in parts or (parts and ":" in parts[0]):
        raise UploadError(f"unzulässiger Pfad im Archiv: {name!r}")
    return parts
