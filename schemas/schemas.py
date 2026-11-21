"""Estructuras de datos compartidas (Pydantic v2).

Separa explicitamente el estado TECNICO de un archivo (FileStatus) de su
cumplimiento LEGAL (RequirementResult / Recommendation).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ----------------------------------------------------------------- Enumeraciones
class DocType(str, Enum):
    ESCRITURAS = "escrituras"
    PREDIAL = "predial"
    ALINEAMIENTO = "alineamiento_numero_oficial"
    USO_SUELO = "uso_suelo"
    DESCONOCIDO = "desconocido"


DOC_TYPE_LABELS = {
    DocType.ESCRITURAS: "Escrituras",
    DocType.PREDIAL: "Predial",
    DocType.ALINEAMIENTO: "Alineamiento y Número Oficial",
    DocType.USO_SUELO: "Uso de Suelo",
    DocType.DESCONOCIDO: "Desconocido",
}


class ProjectType(str, Enum):
    STORE = "store"
    CEDIS = "cedis"


PROJECT_LABELS = {ProjectType.STORE: "Tienda comercial", ProjectType.CEDIS: "Centro de Distribución (CEDIS)"}


class FileStatus(str, Enum):
    """Estado TECNICO del archivo (no implica cumplimiento legal)."""
    RECIBIDO = "recibido"
    FORMATO_VALIDO = "formato_valido"
    FORMATO_INVALIDO = "formato_invalido"
    DUPLICADO = "duplicado"
    ILEGIBLE = "ilegible"
    EN_PROCESAMIENTO = "en_procesamiento"
    ANALIZADO = "analizado"
    PENDIENTE_REVISION = "pendiente_revision"


class Verdict(str, Enum):
    CUMPLE = "cumple"
    REQUIERE_REVISION = "requiere_revision"
    NO_CUMPLE = "no_cumple"


class RequirementStatus(str, Enum):
    CUMPLIDO = "cumplido"
    INCUMPLIDO = "incumplido"
    PENDIENTE = "pendiente"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AuditStatus(str, Enum):
    NO_AUDITADO = "no_auditado"
    VERIFICADO = "verificado"
    NO_VERIFICADO = "no_verificado"
    REQUIERE_REVISION = "requiere_revision"


class ExpedienteStatus(str, Enum):
    RECIBIDO = "recibido"
    EN_ANALISIS = "en_analisis"
    ANALIZADO = "analizado"
    ERROR = "error"
    PURGADO = "purgado"


# ---------------------------------------------------------------- Evidencia
class Evidence(BaseModel):
    """Cita literal con su ubicacion en el documento original."""
    file_id: str
    filename: str
    page: Optional[int] = Field(None, description="Pagina 1-indexada")
    fragment: str = Field(..., description="Texto literal copiado del documento")
    verified: Optional[bool] = Field(None, description="Lo fija el Auditor (Agente 9)")


class ExtractedField(BaseModel):
    """Un dato extraido, siempre con su evidencia."""
    value: Optional[str] = None
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    status: Literal["present", "missing", "ambiguous", "illegible"] = "missing"


# ----------------------------------------------------------- Archivos / recepcion
class Rejection(BaseModel):
    """Motivo de rechazo: que paso, por que, y que puede hacer el usuario."""
    code: str
    message: str
    hint: str


class FileRecord(BaseModel):
    file_id: str
    original_name: str
    source: str = "upload"          # "upload" o "zip:<nombre.zip>"
    detected_format: Optional[str] = None
    size_bytes: int = 0
    sha256: Optional[str] = None
    status: FileStatus = FileStatus.RECIBIDO
    rejection: Optional[Rejection] = None
    duplicate_of: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    declared_category: Optional[DocType] = None   # lo que dijo el usuario en el formulario
    doc_type: DocType = DocType.DESCONOCIDO         # lo que concluye el Agente 2
    classification_confidence: float = 0.0
    pages: int = 0


# ---------------------------------------------------------------- Hallazgos
class Finding(BaseModel):
    finding_id: str
    description: str
    finding_type: str
    severity: Severity = Severity.MEDIUM
    file_id: Optional[str] = None
    filename: Optional[str] = None
    page: Optional[int] = None
    fragment: Optional[str] = None
    found_value: Optional[str] = None
    compared_value: Optional[str] = None
    rule: Optional[str] = None
    agent: str
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    audit_status: AuditStatus = AuditStatus.NO_AUDITADO
    evidence: list[Evidence] = Field(default_factory=list)


class RequirementResult(BaseModel):
    requirement_id: str
    description: str
    status: RequirementStatus
    justification: str
    rule: Optional[str] = None
    rule_level: Optional[str] = None        # oficial / provisional ...
    evidence: list[Evidence] = Field(default_factory=list)


# ------------------------------------------------------------ Trazabilidad agentes
class AgentLogEntry(BaseModel):
    agent_id: str
    agent_name: str
    started_at: datetime
    duration_ms: int
    status: Literal["ok", "error", "timeout", "skipped"]
    input_summary: str = ""
    output_summary: str = ""
    error: Optional[str] = None
    retries: int = 0
    used_llm: bool = False


class FinalRecommendation(BaseModel):
    model_config = ConfigDict(use_enum_values=False)
    verdict: Verdict
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    explanation: str
    reason: str
    fulfilled: list[str] = Field(default_factory=list)
    unfulfilled: list[str] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)
    inconsistencies: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    disclaimer: str = "Recomendación inicial automatizada. No sustituye la decisión del abogado."


# ------------------------------------------------------- Contenido de documentos
class PageContent(BaseModel):
    """Texto de una pagina con su origen y su estado de legibilidad."""
    page: int                                   # 1-indexada
    text: str = ""
    source: Literal["native", "ocr", "none"] = "none"
    ocr_confidence: Optional[float] = None
    # ok = legible | baja_calidad = texto extraido con advertencias | ilegible = no confiable
    # en_blanco = sin contenido | omitida = no analizada (limite o tiempo)
    status: Literal["ok", "baja_calidad", "ilegible", "en_blanco", "omitida"] = "ok"
    issues: list[str] = Field(default_factory=list)
    quality: dict = Field(default_factory=dict)
    rotation_applied: int = 0


class ProcessedDocument(BaseModel):
    file_id: str
    filename: str
    pages_total: int = 0
    pages: list[PageContent] = Field(default_factory=list)
    ocr_engine: Optional[str] = None
    elapsed_ms: int = 0

    def page_text(self, page: int) -> str:
        for p in self.pages:
            if p.page == page:
                return p.text
        return ""

    def full_text(self, max_chars: Optional[int] = None) -> str:
        out = "\n".join(f"[Página {p.page}]\n{p.text}" for p in self.pages if p.text.strip())
        return out[:max_chars] if max_chars else out

    @property
    def unreadable_pages(self) -> list[int]:
        return [p.page for p in self.pages if p.status == "ilegible"]

    @property
    def has_text(self) -> bool:
        return any(p.text.strip() for p in self.pages)
