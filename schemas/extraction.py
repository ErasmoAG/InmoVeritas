"""Salidas estructuradas de los agentes (lo que el LLM debe devolver)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class LLMField(BaseModel):
    """El orden importa: primero se copia el fragmento y luego se deduce el valor (mejora la fiabilidad del modelo)."""
    fragment: Optional[str] = Field(None, description="Fragmento COPIADO TEXTUALMENTE del documento que contiene el dato (maximo 200 caracteres); null si el dato no aparece")
    page: Optional[int] = Field(None, description="Numero de pagina donde aparece el fragmento (segun las marcas [Pagina N])")
    value: Optional[str] = Field(None, description="El dato concreto contenido en el fragmento (OBLIGATORIO si hay fragmento); null solo si el dato no aparece")


class ClassificationOut(BaseModel):
    doc_type: Literal["escrituras", "predial", "alineamiento_numero_oficial", "uso_suelo", "desconocido"]
    confidence: float = Field(..., ge=0, le=1, description="Confianza entre 0.0 y 1.0 (por ejemplo 0.85, NO porcentaje)")
    reasoning: str = Field(..., description="Una frase explicando que partes del contenido justifican el tipo")

    @field_validator("confidence", mode="before")
    @classmethod
    def _scale(cls, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        return v / 100 if v > 1 else v
    fragment: Optional[str] = Field(None, description="Fragmento textual que mejor identifica el tipo de documento")


class EscrituraOut(BaseModel):
    owner: LLMField = Field(default_factory=LLMField, description="Propietario o titular actual / adquirente del inmueble")
    property_address: LLMField = Field(default_factory=LLMField, description="Direccion o ubicacion del inmueble")
    property_id: LLMField = Field(default_factory=LLMField, description="Identificacion del predio: lote, manzana, clave catastral, folio real")
    land_surface_m2: LLMField = Field(default_factory=LLMField, description="Superficie del terreno en metros cuadrados")
    deed_number: LLMField = Field(default_factory=LLMField, description="Numero de escritura/instrumento y fecha")
    notary: LLMField = Field(default_factory=LLMField, description="Notario y numero de notaria")
    other_details: LLMField = Field(default_factory=LLMField, description="Otro dato relevante (medidas y colindancias, gravamenes, antecedentes)")


class PredialOut(BaseModel):
    owner: LLMField = Field(default_factory=LLMField, description="Propietario o contribuyente")
    address: LLMField = Field(default_factory=LLMField, description="Direccion o ubicacion del inmueble")
    predial_account: LLMField = Field(default_factory=LLMField, description="Cuenta o clave predial/catastral")
    registered_surface_m2: LLMField = Field(default_factory=LLMField, description="Superficie registrada (terreno) en m2")
    fiscal_year: LLMField = Field(default_factory=LLMField, description="Ejercicio fiscal o ano al que corresponde el predial (4 digitos)")
    amount_status: LLMField = Field(default_factory=LLMField, description="Importe o estado del pago, si aparece")


class AlineamientoOut(BaseModel):
    official_address: LLMField = Field(default_factory=LLMField, description="Direccion oficial del inmueble")
    official_number: LLMField = Field(default_factory=LLMField, description="Numero oficial asignado")
    property_id: LLMField = Field(default_factory=LLMField, description="Identificacion del predio: clave catastral, lote, manzana")
    alignment: LLMField = Field(default_factory=LLMField, description="Alineamiento determinado por la autoridad (afectaciones, restricciones, derechos de via)")
    issue_date: LLMField = Field(default_factory=LLMField, description="Fecha de emision")
    validity: LLMField = Field(default_factory=LLMField, description="Vigencia (fecha o plazo), si el documento la declara")
    issuing_authority: LLMField = Field(default_factory=LLMField, description="Autoridad emisora")
    owner: LLMField = Field(default_factory=LLMField, description="Propietario o solicitante, si aparece")
    surface_m2: LLMField = Field(default_factory=LLMField, description="Superficie del predio en m2, si aparece")


class Compatibility(BaseModel):
    verdict: Literal["compatible", "incompatible", "indeterminado"] = Field(
        ..., description="Compatibilidad del uso autorizado con el destino solicitado, SOLO segun lo que dice el documento")
    reasoning: str = Field(..., description="Explicacion breve basada en el texto")
    page: Optional[int] = None
    fragment: Optional[str] = Field(None, description="Fragmento textual que sustenta el veredicto")


class UsoSueloOut(BaseModel):
    address: LLMField = Field(default_factory=LLMField, description="Direccion del inmueble")
    property_id: LLMField = Field(default_factory=LLMField, description="Identificacion del predio: clave catastral, lote, manzana")
    authorized_use: LLMField = Field(default_factory=LLMField, description="Uso de suelo autorizado / giro")
    permitted_activities: LLMField = Field(default_factory=LLMField, description="Actividades permitidas")
    restrictions: LLMField = Field(default_factory=LLMField, description="Restricciones y condicionantes")
    applicable_surface: LLMField = Field(default_factory=LLMField, description="Superficie aplicable en m2")
    issue_date: LLMField = Field(default_factory=LLMField, description="Fecha de emision")
    validity: LLMField = Field(default_factory=LLMField, description="Vigencia, si el documento la declara")
    issuing_authority: LLMField = Field(default_factory=LLMField, description="Autoridad emisora")
    owner: LLMField = Field(default_factory=LLMField, description="Propietario o solicitante, si aparece")
    compatibility: Compatibility


class ComparisonJudgment(BaseModel):
    same: Literal["igual", "distinto", "indeterminado"]
    reasoning: str


class AuditJudgment(BaseModel):
    supported: Literal["si", "no", "dudoso"]
    reasoning: str


class NarrativeOut(BaseModel):
    explanation: str = Field(..., description="Explicacion clara para un abogado, en 3 a 6 oraciones, sin inventar datos")


class DocumentExtraction(BaseModel):
    """Resultado normalizado de un extractor (Agentes 3 a 6) para un documento."""
    file_id: str
    filename: str
    doc_type: str
    fields: dict = Field(default_factory=dict)          # nombre -> schemas.ExtractedField (dict)
    compatibility: Optional[dict] = None                # solo uso_suelo: veredicto para el destino
    legibility_notes: list[str] = Field(default_factory=list)
    status: Literal["ok", "parcial", "sin_texto", "error"] = "ok"
    error: Optional[str] = None
