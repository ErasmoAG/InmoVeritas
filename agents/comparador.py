"""Agente 8 - Comparador y detector de inconsistencias entre documentos del mismo expediente.

Compara con reglas deterministas (identificadores, superficies) y consulta al LLM solo en los casos
ambiguos (nombres y direcciones redactados distinto). Una diferencia NO se trata automaticamente como incumplimiento."""
from __future__ import annotations

import time
from difflib import SequenceMatcher
from typing import Optional

from agents.base import BaseAgent, squash
from agents.common import get_field, parse_surface
from schemas.extraction import ComparisonJudgment
from schemas.schemas import AuditStatus, Evidence, Finding, Severity
from services.rules_service import LoadedRules

FIELDS = {
    "address": ("Dirección del inmueble", [("escrituras", "property_address"), ("predial", "address"), ("alineamiento_numero_oficial", "official_address"), ("uso_suelo", "address")]),
    "owner": ("Propietario o titular", [("escrituras", "owner"), ("predial", "owner"), ("alineamiento_numero_oficial", "owner"), ("uso_suelo", "owner")]),
    "surface": ("Superficie (m²)", [("escrituras", "land_surface_m2"), ("predial", "registered_surface_m2"), ("alineamiento_numero_oficial", "surface_m2"), ("uso_suelo", "applicable_surface")]),
    "parcel_id": ("Identificación del predio / clave", [("escrituras", "property_id"), ("predial", "predial_account"), ("alineamiento_numero_oficial", "property_id"), ("uso_suelo", "property_id")]),
}
DOC_LABEL = {"escrituras": "Escrituras", "predial": "Predial", "alineamiento_numero_oficial": "Alineamiento", "uso_suelo": "Uso de suelo"}
MAX_LLM_CALLS = 8


class ComparadorAgent(BaseAgent):
    agent_id = "A8"
    name = "Comparador de documentos"
    responsibility = "Detectar diferencias de dirección, titular, superficie e identificación del predio entre documentos, con evidencias."
    system_prompt = (
        "Eres un abogado que compara datos de un mismo inmueble tomados de documentos distintos (el texto viene de OCR y puede tener "
        "errores o carecer de espacios). Decide si los dos valores se refieren a lo mismo: 'igual' si son la misma persona/direccion aunque "
        "cambie el orden, las abreviaturas, las mayusculas o haya errores menores de OCR; 'distinto' si claramente son personas o lugares "
        "diferentes; 'indeterminado' si no es posible concluir. Explica en una frase. No inventes."
    )
    human = "Dato: {label}\nDocumento A ({da}): {va}\nDocumento B ({db}): {vb}"

    def _same_text(self, kind: str, a: str, b: str, da: str, db: str, label: str, budget: list) -> str:
        sa, sb = squash(a), squash(b)
        if not sa or not sb:
            return "indeterminado"
        if sa == sb or sa in sb or sb in sa:
            return "igual"
        if sorted(sa) == sorted(sb) or SequenceMatcher(None, "".join(sorted(sa)), "".join(sorted(sb))).ratio() >= 0.97:
            return "igual"  # mismo conjunto de letras: orden de nombres distinto
        ratio = SequenceMatcher(None, sa, sb).ratio()
        if ratio >= 0.92:
            return "igual"
        if budget[0] <= 0:
            return "indeterminado"
        budget[0] -= 1
        try:
            return self.invoke_llm(ComparisonJudgment, self.human, label=label, da=da, va=a, db=db, vb=b).same
        except Exception:
            return "indeterminado"

    def run(self, extractions: list[dict], rules: LoadedRules) -> dict:
        t0, started = time.monotonic(), self.now()
        cc = rules.rules.cross_checks
        tol = rules.rules.tolerances
        comparisons, findings = [], []
        budget = [MAX_LLM_CALLS]
        n = 0
        for key, (label, sources) in FIELDS.items():
            cfg = getattr(cc, {"address": "address", "owner": "owner", "surface": "surface", "parcel_id": "parcel_id"}[key])
            if not cfg.enabled:
                continue
            entries = []
            for dt, fname in sources:
                for ex, f in get_field(extractions, dt, fname):
                    entries.append({"doc_type": dt, "doc": DOC_LABEL[dt], "file_id": ex["file_id"], "filename": ex["filename"], "value": f["value"], "evidence": f["evidence"], "confidence": f["confidence"]})
            if len(entries) < 2:
                comparisons.append({"field": key, "label": label, "values": entries, "result": "sin_comparacion", "note": "Se necesitan al menos dos documentos con este dato."})
                continue
            ref, results = entries[0], []
            for other in entries[1:]:
                if key == "parcel_id":
                    da, db = "".join(c for c in squash(ref["value"]) if c.isdigit()), "".join(c for c in squash(other["value"]) if c.isdigit())
                    res = "igual" if da and db and (da == db or da in db or db in da) else ("distinto" if da and db else "indeterminado")
                    note = ""
                elif key == "surface":
                    sa, sb = parse_surface(ref["value"]), parse_surface(other["value"])
                    note = ""
                    if sa is None or sb is None:
                        res = "indeterminado"
                    elif abs(sa - sb) < 0.01:
                        res = "igual"
                    else:
                        diff, pct = abs(sa - sb), abs(sa - sb) / max(sa, sb) * 100
                        within = (tol.surface_difference_m2 is not None and diff <= tol.surface_difference_m2) or (tol.surface_difference_percent is not None and pct <= tol.surface_difference_percent)
                        res = "igual" if within else "distinto"
                        note = (f"Diferencia de {diff:.2f} m² ({pct:.1f} %). " + ("Dentro de la tolerancia configurada." if within else
                                "No hay tolerancia oficial configurada: se reporta para revisión, no como incumplimiento." if tol.surface_difference_m2 is None and tol.surface_difference_percent is None else "Excede la tolerancia configurada."))
                else:
                    res, note = self._same_text(key, ref["value"], other["value"], ref["doc"], other["doc"], label, budget), ""
                results.append((other, res, note))
                if res == "distinto":
                    n += 1
                    ev = [Evidence(**e) for e in ref["evidence"] + other["evidence"]]
                    findings.append(Finding(finding_id=f"C-{n:02d}", description=f"{label}: no coincide entre {ref['doc']} y {other['doc']}. {note}".strip(), finding_type="inconsistency",
                                            severity=Severity(cfg.severity_on_mismatch), file_id=other["file_id"], filename=other["filename"], page=ev[-1].page if ev else None, fragment=ev[-1].fragment if ev else None,
                                            found_value=f"{ref['doc']}: {ref['value']}", compared_value=f"{other['doc']}: {other['value']}", rule=f"cross_checks.{key}", agent=self.agent_id,
                                            confidence=min(ref["confidence"], other["confidence"]), evidence=ev))
            overall = "distinto" if any(r == "distinto" for _, r, _ in results) else ("indeterminado" if any(r == "indeterminado" for _, r, _ in results) else "igual")
            comparisons.append({"field": key, "label": label, "values": entries, "result": overall, "note": " ".join(x for _, _, x in results if x)})
        self.record(t0, started, "ok", f"{len(extractions)} extracciones", f"{len(comparisons)} comparaciones, {len(findings)} inconsistencias", used_llm=budget[0] < MAX_LLM_CALLS)
        return {"comparisons": comparisons, "findings": findings}
