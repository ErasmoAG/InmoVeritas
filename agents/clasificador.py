"""Agente 2 - Clasificador documental: identifica el tipo por el CONTENIDO real (no por nombre/extension)."""
from __future__ import annotations

import time
from typing import Optional

from agents.base import BaseAgent, squash
from schemas.extraction import ClassificationOut
from schemas.schemas import DocType, Evidence, ProcessedDocument

# Pistas lexicas: solo orientan al modelo (se le muestran como "indicios"); la decision es del LLM.
HINTS = {
    DocType.ESCRITURAS: ["escritura", "notario", "notaria", "compraventa", "protocolo", "instrumento", "volumen", "otorga", "adquirente", "enajenante"],
    DocType.PREDIAL: ["predial", "impuesto predial", "clave catastral", "contribuyente", "tesoreria", "recibo oficial de pago", "ejercicio fiscal", "valor catastral"],
    DocType.ALINEAMIENTO: ["alineamiento", "numero oficial", "constancia de alineamiento", "desarrollo urbano", "restriccion", "afectacion"],
    DocType.USO_SUELO: ["uso de suelo", "licencia de uso", "compatibilidad urbanistica", "zonificacion", "giro", "densidad", "coeficiente de ocupacion"],
}


class ClasificadorAgent(BaseAgent):
    agent_id = "A2"
    name = "Clasificador documental"
    responsibility = "Identificar el tipo documental mediante el contenido real del documento y asignar confianza."
    system_prompt = (
        "Eres un clasificador de documentos legales y administrativos de inmuebles en Mexico. "
        "Clasifica el documento SOLO por su contenido (ignora el nombre del archivo). Categorias: "
        "escrituras (escritura publica ante notario, compraventa, titulo de propiedad), "
        "predial (recibo o boleta de impuesto predial / tesoreria), "
        "alineamiento_numero_oficial (constancia de alineamiento y/o numero oficial emitida por desarrollo urbano), "
        "uso_suelo (licencia, constancia o dictamen de uso de suelo), desconocido (cualquier otra cosa o texto insuficiente). "
        "El texto proviene de OCR y puede carecer de espacios o tener errores. Si el contenido no basta para decidir, "
        "responde desconocido con confianza baja. No inventes. El fragmento debe ser copia literal del texto."
    )

    @staticmethod
    def hint_scores(text: str) -> dict[str, int]:
        sq = squash(text)
        return {t.value: sum(sq.count(squash(k)) for k in kws) for t, kws in HINTS.items()}

    def run(self, doc: ProcessedDocument) -> dict:
        t0, started = time.monotonic(), self.now()
        try:
            sample = ""
            for p in doc.pages:
                if p.text.strip():
                    sample += f"[Página {p.page}]\n{p.text.strip()}\n"
                if len(sample) > 3500:
                    break
            sample = sample[:3500]
            if not sample.strip():
                res = {"file_id": doc.file_id, "doc_type": DocType.DESCONOCIDO, "confidence": 0.0,
                       "reasoning": "No hay texto legible para clasificar.", "evidence": None}
                self.record(t0, started, "ok", doc.filename, "desconocido (sin texto)", used_llm=False)
                return res
            hints = self.hint_scores(sample)
            out = self.invoke_llm(
                ClassificationOut,
                "Indicios por coincidencia de palabras (solo orientativos): {hints}\n\nTEXTO DEL DOCUMENTO:\n{text}",
                hints=hints, text=sample)
            ev: Optional[Evidence] = None
            if out.fragment:
                page = next((p.page for p in doc.pages if squash(out.fragment) and squash(out.fragment) in squash(p.text)), None)
                ev = Evidence(file_id=doc.file_id, filename=doc.filename, page=page, fragment=out.fragment)
            conf = out.confidence
            top = max(hints, key=hints.get)
            if conf < 0.5 and out.doc_type == top and hints[top] >= 3:
                conf = 0.8  # el modelo y los indicios lexicos coinciden: dos senales independientes
                out.reasoning += " (coincide con los indicios del texto)"
            elif conf < 0.5:
                conf = min(conf, 0.4)
            if any(p.status == "baja_calidad" for p in doc.pages):
                conf = min(conf, 0.8)
            res = {"file_id": doc.file_id, "doc_type": DocType(out.doc_type), "confidence": round(conf, 2),
                   "reasoning": out.reasoning, "evidence": ev}
            self.record(t0, started, "ok", doc.filename, f"{out.doc_type} ({conf:.2f})")
            return res
        except Exception as exc:
            self.record(t0, started, "error", doc.filename, error=str(exc))
            return {"file_id": doc.file_id, "doc_type": DocType.DESCONOCIDO, "confidence": 0.0,
                    "reasoning": f"No se pudo clasificar: {exc}", "evidence": None, "error": str(exc)}
