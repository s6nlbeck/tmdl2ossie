"""Validate Ossie documents against the Apache Ossie specification.

A thin facade over the vendored upstream validator in
``vendor/ossie/validation/validate.py``. That file is a CLI: its five checks are
clean, importable functions, but the aggregation lives in ``main()`` together
with ``print()`` and ``sys.exit()``. We therefore call the checks ourselves and
leave the vendored tree byte-identical to upstream, so it stays swappable.

Validating here rather than relying on the ``ossie`` pydantic models is
deliberate. Those models are more permissive than the specification: unknown
properties are accepted in nested objects (and silently dropped), and ``version``
is neither required nor value-checked. The upstream validator goes against the
JSON Schema and adds four semantic checks that no schema can express -- unique
names, relationship references, column arity and SQL syntax.

Note that DAX expressions are *not* syntax-checked: sqlglot cannot parse DAX, so
upstream skips that dialect. Only portable SQL dialects get real scrutiny.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ossie import OssieDocument

VENDOR_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "ossie"
SCHEMA_PATH = VENDOR_ROOT / "core-spec" / "ossie-schema.json"
VALIDATOR_PATH = VENDOR_ROOT / "validation" / "validate.py"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of validating one document, with warnings kept apart from errors."""

    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        """Whether the document is valid. Warnings do not make it invalid."""
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok

    def report(self) -> str:
        """Render all diagnostics as one human-readable block."""
        lines = [f"error: {message}" for message in self.errors]
        lines += [f"warning: {message}" for message in self.warnings]
        return "\n".join(lines) or "valid"


@cache
def _upstream() -> ModuleType:
    """Load the vendored validator by path.

    It is a standalone script, not an installed package, so there is no module
    path to import. Loading it by file location keeps the vendored tree free of
    the ``__init__.py`` files an import would otherwise need.
    """
    spec = importlib.util.spec_from_file_location("ossie_upstream_validate", VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the vendored Ossie validator from {VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@cache
def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_document(data: dict[str, Any]) -> ValidationResult:
    """Validate an already-loaded Ossie document."""
    upstream = _upstream()
    diagnostics = upstream.validate_schema(data, _schema())

    # Mirror upstream's ordering: the semantic checks assume a well-formed
    # document, so they only run once the structure itself is sound.
    if not diagnostics and isinstance(data, dict) and "datasets" in data:
        diagnostics += upstream.validate_unique_names(data)
        diagnostics += upstream.validate_references(data)
        diagnostics += upstream.validate_relationship_column_arity(data)
        diagnostics += upstream.validate_sql(data)

    # Upstream marks non-fatal diagnostics by type, not by message text.
    warnings = [d for d in diagnostics if isinstance(d, upstream.ValidationWarning)]
    errors = [d for d in diagnostics if not isinstance(d, upstream.ValidationWarning)]
    return ValidationResult(errors=errors, warnings=warnings)


def validate_model(model: OssieDocument) -> ValidationResult:
    """Validate a document we built with the pydantic models.

    Goes through the model's own JSON serialization so that enums, field aliases
    (``from_dataset`` -> ``from``) and omitted empty values are exactly what we
    would write to disk.
    """
    return validate_document(json.loads(model.to_ossie_json()))


def validate_file(path: str | Path) -> ValidationResult:
    """Validate an Ossie YAML or JSON file.

    Uses upstream's loader, which rejects duplicate mapping keys -- YAML would
    otherwise silently keep the last one.
    """
    import yaml

    try:
        data = load_yaml(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        return ValidationResult(errors=[f"[YAML] {error}"], warnings=[])
    return validate_document(data)


def load_yaml(text: str) -> Any:
    """Parse Ossie YAML the way upstream does, rejecting duplicate mapping keys.

    Raises ``yaml.YAMLError`` on malformed input.
    """
    import yaml

    return yaml.load(text, Loader=_upstream().UniqueKeyLoader)
