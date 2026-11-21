"""Base comun de los extractores especializados (Agentes 3 a 6)."""
from __future__ import annotations

import time
from typing import Optional, Type

from pydantic import BaseModel

from agents.base import BaseAgent, squash
from agents.context import select_context
from schemas.extraction import DocumentExtraction, LLMField
from schemas.schemas import Evidence, ExtractedField, ProcessedDocument

COMMON_RULES = (
    "Reglas estrictas: (1) Extrae SOLO lo que aparece en el texto; si un dato no aparece, deja fragment, page y value en null. Si encuentras el dato, SIEMPRE llena value con el dato concreto (no lo dejes en null). "
    "(2) 'fragment' debe ser una copia LITERAL y corta (maximo 200 caracteres) del texto que respalda el valor, sin corregir ni resumir. "
    "(3) 'page' es el numero de la marca [Página N] bajo la que aparece el fragmento. "
    "(4) El texto viene de OCR: puede faltar espacios o haber errores; copia el fragmento tal como aparece. "
    "(5) Nunca inventes ni completes datos ilegibles. (6) Si hay varios valores distintos, elige el que corresponde al inmueble principal."
)


class ExtractorAgent(BaseAgent):
    doc_type = ""
    schema: Type[BaseModel]
    keywords: list[str] = []
    human = "TEXTO DEL DOCUMENTO (con marcas de pagina):\n{text}"

    def extra_vars(self, **kw) -> dict:
        return {}

    @classmethod
    def _guide(cls) -> str:
        """El esquema JSON restringe la salida pero el modelo no ve las descripciones: se incluyen en el prompt."""
        lines = []
        for name, f in cls.schema.model_fields.items():
            if name == "compatibility":
                lines.append("compatibility: veredicto de compatibilidad con el destino solicitado (compatible/incompatible/indeterminado), con explicacion y fragmento")
            else:
                desc = ""
                ann = f.annotation
                inner = getattr(ann, "model_fields", {}).get("value")
                lines.append(f"{name}: {f.description or (inner.description if inner else '')}")
        return "\n".join(lines)

    def run(self, doc: ProcessedDocument, **kw) -> DocumentExtraction:
        t0, started = time.monotonic(), self.now()
        notes = [f"Página {p.page}: {m}" for p in doc.pages for m in p.issues if p.status in ("ilegible", "baja_calidad", "omitida")]
        text = select_context(doc, self.keywords)
        if not text.strip():
            self.record(t0, started, "skipped", doc.filename, "sin texto legible", used_llm=False)
            return DocumentExtraction(file_id=doc.file_id, filename=doc.filename, doc_type=self.doc_type, status="sin_texto",
                                      legibility_notes=notes or ["No se pudo leer texto del documento."])
        try:
            out = self.invoke_llm(self.schema, self.human + "\n\nCAMPOS A EXTRAER (nombre: que buscar). Responde TODOS los que aparezcan:\n{guide}",
                                  text=text, guide=self._guide(), **self.extra_vars(**kw))
        except Exception as exc:
            self.record(t0, started, "error", doc.filename, error=str(exc))
            return DocumentExtraction(file_id=doc.file_id, filename=doc.filename, doc_type=self.doc_type, status="error",
                                      error=str(exc), legibility_notes=notes)
        low_pages = {p.page for p in doc.pages if p.status == "baja_calidad"}
        page_sq = {p.page: squash(p.text) for p in doc.pages}
        fields: dict[str, dict] = {}
        compat = None
        for name, value in out:
            if isinstance(value, LLMField):
                fields[name] = self._to_field(doc, value, low_pages, page_sq).model_dump()
            elif name == "compatibility" and value is not None:
                ev = None
                if value.fragment:
                    pg = self._locate(value.fragment, value.page, page_sq)
                    ev = Evidence(file_id=doc.file_id, filename=doc.filename, page=pg, fragment=value.fragment).model_dump()
                compat = {"verdict": value.verdict, "reasoning": value.reasoning, "evidence": ev}
        found = sum(1 for f in fields.values() if f["status"] == "present")
        if found < max(2, int(0.3 * len(fields))):  # el modelo dejo casi todo vacio: segundo intento con un esquema plano
            try:
                fields = self._fallback(doc, text, fields, low_pages, page_sq, **kw)
                found = sum(1 for f in fields.values() if f["status"] == "present")
            except Exception:
                pass
        self.record(t0, started, "ok", doc.filename, f"{found}/{len(fields)} campos")
        return DocumentExtraction(file_id=doc.file_id, filename=doc.filename, doc_type=self.doc_type, fields=fields, compatibility=compat,
                                  status="ok" if found else "parcial", legibility_notes=notes)

    def _fallback(self, doc, text, fields, low_pages, page_sq, **kw) -> dict:
        """Segundo intento: el modelo devuelve solo valores (esquema plano); la evidencia se localiza de forma
        determinista buscando el valor en el texto original (la linea donde aparece)."""
        from pydantic import create_model
        spec = {n: (Optional[str], None) for n in self.schema.model_fields if n != "compatibility"}
        Flat = create_model("Flat" + self.schema.__name__, **spec)
        out = self.invoke_llm(Flat, self.human + "\n\nDevuelve un JSON con estos campos (texto breve copiado del documento, o null si no aparece):\n{guide}",
                              text=text, guide=self._guide(), **self.extra_vars(**kw))
        lines = [(p.page, ln) for p in doc.pages for ln in p.text.split("\n") if ln.strip()]
        for name, val in out:
            val = (val or "").strip()
            if not val or fields.get(name, {}).get("status") == "present":
                continue
            vsq = squash(val)
            hit = next(((pg, ln) for pg, ln in lines if vsq and vsq in squash(ln)), None)
            if hit:
                ev = Evidence(file_id=doc.file_id, filename=doc.filename, page=hit[0], fragment=hit[1].strip()[:200])
                conf = 0.7 if hit[0] in low_pages else 0.8
                fields[name] = ExtractedField(value=val, evidence=[ev], confidence=conf, status="present").model_dump()
        return fields

    @staticmethod
    def _locate(fragment: str, hinted: Optional[int], page_sq: dict) -> Optional[int]:
        """Pagina donde realmente aparece el fragmento (se corrige la que diga el modelo)."""
        fsq = squash(fragment)
        if not fsq:
            return hinted
        if hinted in page_sq and fsq in page_sq[hinted]:
            return hinted
        for pg, txt in page_sq.items():
            if fsq in txt:
                return pg
        return hinted

    @classmethod
    def _to_field(cls, doc: ProcessedDocument, f: LLMField, low_pages: set, page_sq: dict) -> ExtractedField:
        value = (f.value or "").strip() or None
        if not value:
            return ExtractedField(status="missing")
        ev = []
        conf = 0.9
        if f.fragment and f.fragment.strip():
            ev.append(Evidence(file_id=doc.file_id, filename=doc.filename, page=cls._locate(f.fragment, f.page, page_sq), fragment=f.fragment.strip()))
        else:
            conf = 0.4  # sin cita textual: el auditor lo marcara
        if ev and ev[0].page in low_pages:
            conf = min(conf, 0.7)
        return ExtractedField(value=value, evidence=ev, confidence=conf, status="present")
