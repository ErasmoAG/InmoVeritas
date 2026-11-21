"""Carga, validacion y actualizacion del archivo externo de reglas (YAML).

- Valida la estructura (Pydantic, extra='forbid' para detectar claves mal escritas).
- Calcula una huella SHA-256 para citar la version exacta usada en cada analisis.
- Registra cada actualizacion en storage/rules_history.jsonl (sin datos personales).
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from settings import get_settings

Level = Literal["oficial_comite", "criterio_oficial_reto", "confirmado_tiendas_3b", "provisional_mvp"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Meta(_Strict):
    version: str
    description: str = ""
    source_priority: list[str] = Field(default_factory=list)


class DocumentsRule(_Strict):
    required: list[str]
    missing_document_outcome: Literal["requires_review", "not_compliant"] = "requires_review"
    source: str = ""
    level: Level = "provisional_mvp"


class PredialRule(_Strict):
    enabled: bool = True
    min_year: Optional[int] = None
    source: str = ""
    level: Level = "provisional_mvp"
    requires_committee_confirmation: bool = True


class ValidityRule(_Strict):
    enabled: bool = True
    documents_with_validity: list[str] = Field(default_factory=list)
    default_validity_days: Optional[int] = None
    source: str = ""
    level: Level = "provisional_mvp"


class TolerancesRule(_Strict):
    surface_difference_percent: Optional[float] = None
    surface_difference_m2: Optional[float] = None
    source: str = ""
    level: Level = "provisional_mvp"


class Hints(_Strict):
    positive: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)


class LandUseRule(_Strict):
    allowed_categories: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    provisional_hints: Hints = Field(default_factory=Hints)
    source: str = ""
    level: Level = "provisional_mvp"
    requires_committee_confirmation: bool = True


class LandUse(_Strict):
    store: LandUseRule
    cedis: LandUseRule


class CrossCheck(_Strict):
    enabled: bool = True
    severity_on_mismatch: Literal["low", "medium", "high"] = "medium"


class CrossChecks(_Strict):
    address: CrossCheck
    owner: CrossCheck
    surface: CrossCheck
    official_number: CrossCheck
    parcel_id: CrossCheck


class ValidationRule(_Strict):
    require_evidence: bool = True
    unknown_data_status: Literal["pending", "requires_review"] = "pending"
    missing_data_status: Literal["pending", "requires_review"] = "requires_review"
    min_extraction_confidence: float = 0.5


class RecommendationRule(_Strict):
    not_compliant_if_any: list[str] = Field(default_factory=list)
    compliant_requires_no_pending: bool = True
    default: Literal["requires_review", "not_compliant", "compliant"] = "requires_review"


class Rules(_Strict):
    property_types: list[Literal["store", "cedis"]]
    documents: DocumentsRule
    predial: PredialRule
    validity: ValidityRule
    tolerances: TolerancesRule
    land_use: LandUse
    cross_checks: CrossChecks
    validation: ValidationRule
    finding_classification: dict[str, Literal["low", "medium", "high"]]
    recommendation: RecommendationRule


class RulesConfig(_Strict):
    meta: Meta
    rules: Rules


class RulesError(ValueError):
    """Error de estructura o sintaxis en el YAML de reglas (mensaje legible)."""


class LoadedRules(BaseModel):
    config: RulesConfig
    sha256: str
    version: str
    path: str
    loaded_at: str

    @property
    def rules(self) -> Rules:
        return self.config.rules

    def reference(self) -> dict:
        """Referencia que se guarda con cada analisis."""
        return {"version": self.version, "sha256": self.sha256, "loaded_at": self.loaded_at}


def validate_yaml_text(text: str) -> RulesConfig:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesError(f"El archivo no es un YAML válido: {exc}") from exc
    if not isinstance(data, dict):
        raise RulesError("El archivo de reglas debe ser un mapa YAML con las secciones 'meta' y 'rules'.")
    try:
        return RulesConfig.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            problems.append(f"- {loc}: {err['msg']}")
        raise RulesError("El archivo de reglas tiene errores de estructura:\n" + "\n".join(problems)) from exc


def load_rules(path: Optional[Path] = None) -> LoadedRules:
    path = Path(path) if path else get_settings().rules_file
    raw = path.read_bytes()
    cfg = validate_yaml_text(raw.decode("utf-8"))
    return LoadedRules(
        config=cfg,
        sha256=hashlib.sha256(raw).hexdigest(),
        version=cfg.meta.version,
        path=str(path),
        loaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def _history_path() -> Path:
    return get_settings().storage_dir / "rules_history.jsonl"


def update_rules(new_text: str, actor: str = "abogado", path: Optional[Path] = None) -> LoadedRules:
    """Valida y reemplaza el archivo de reglas. Conserva copia de la version anterior
    y registra la actualizacion. Si el YAML es invalido NO se modifica nada."""
    cfg = validate_yaml_text(new_text)  # lanza RulesError si falla
    path = Path(path) if path else get_settings().rules_file
    history = _history_path()
    history.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if path.exists():
        backup_dir = history.parent / "rules_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_dir / f"rules_config_{stamp}.yaml")
    raw = new_text.encode("utf-8")
    path.write_bytes(raw)  # bytes exactos: la huella SHA-256 coincide con el archivo en disco
    sha = hashlib.sha256(raw).hexdigest()
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"updated_at": stamp, "actor": actor, "version": cfg.meta.version, "sha256": sha}) + "\n")
    return load_rules(path)


def rules_history() -> list[dict]:
    h = _history_path()
    if not h.exists():
        return []
    return [json.loads(line) for line in h.read_text(encoding="utf-8").splitlines() if line.strip()]
