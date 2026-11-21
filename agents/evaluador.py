"""Agente 10 - Evaluador y generador de recomendaciones (Cumple / Requiere revision / No cumple)."""
from __future__ import annotations

import time
from typing import Optional

from agents.base import BaseAgent
from schemas.extraction import NarrativeOut
from schemas.schemas import (AuditStatus, DOC_TYPE_LABELS, DocType, Evidence, ExpedienteStatus, FinalRecommendation, Finding,
                             RequirementResult, RequirementStatus, Verdict)
from services.rules_service import LoadedRules


class EvaluadorAgent(BaseAgent):
    agent_id = "A10"
    name = "Evaluador y recomendación"
    responsibility = "Emitir exclusivamente Cumple, Requiere revisión o No cumple, con explicación, evidencias y nivel de confianza."
    system_prompt = (
        "Eres un asistente legal que redacta, para un abogado de Tiendas 3B, la explicacion de una recomendacion inicial sobre un expediente "
        "inmobiliario. Usa EXCLUSIVAMENTE los datos del resumen; no agregues hechos, no cambies el veredicto y no digas que sustituyes la "
        "decision del abogado. Un dato desconocido es 'pendiente de revision', no un incumplimiento. Redacta en espanol claro, 3 a 6 oraciones."
    )
    human = "VEREDICTO: {verdict}\nDESTINO: {dest}\nRESUMEN ESTRUCTURADO:\n{summary}"

    def run(self, requirements: list[RequirementResult], findings: list[Finding], comparisons: list[dict], audit: dict,
            extractions: list[dict], missing_docs: list[str], unreadable: list[str], project_type, rules: LoadedRules) -> FinalRecommendation:
        t0, started = time.monotonic(), self.now()
        crit = set(rules.rules.recommendation.not_compliant_if_any)
        crit_findings = [f for f in findings if f.finding_type in crit]
        confirmed = [f for f in crit_findings if f.audit_status == AuditStatus.VERIFICADO]
        incumplidos = [r for r in requirements if r.status == RequirementStatus.INCUMPLIDO]
        pendientes = [r for r in requirements if r.status == RequirementStatus.PENDIENTE]
        cumplidos = [r for r in requirements if r.status == RequirementStatus.CUMPLIDO]
        incons = [f for f in findings if f.finding_type == "inconsistency"]
        unverified = [f for f in findings if f.audit_status in (AuditStatus.NO_VERIFICADO, AuditStatus.REQUIERE_REVISION)]

        if confirmed:
            verdict, reason = Verdict.NO_CUMPLE, "Existe al menos un incumplimiento confirmado con evidencia verificada: " + "; ".join(f.description for f in confirmed)[:300]
        elif crit_findings or incumplidos or pendientes or incons or missing_docs or unreadable or unverified or audit.get("needs_review"):
            parts = []
            if crit_findings:
                parts.append("hay un posible incumplimiento cuya evidencia no pudo verificarse")
            if missing_docs:
                parts.append("faltan documentos: " + ", ".join(missing_docs))
            if unreadable:
                parts.append("hay documentos o páginas ilegibles que deben reemplazarse")
            if incons:
                parts.append(f"hay {len(incons)} inconsistencia(s) entre documentos")
            if pendientes:
                parts.append(f"hay {len(pendientes)} requisito(s) pendiente(s) de verificación")
            if incumplidos and not crit_findings:
                parts.append("hay requisitos incumplidos que requieren criterio del abogado")
            if audit.get("needs_review") and not crit_findings:
                parts.append("algunas citas no se pudieron verificar en el documento original")
            verdict, reason = Verdict.REQUIERE_REVISION, "Requiere revisión porque " + "; ".join(parts) + "."
        else:
            verdict, reason = Verdict.CUMPLE, "Todos los requisitos evaluados se cumplen, sin inconsistencias y con evidencia verificada."

        confs = [f["confidence"] for e in extractions for f in e["fields"].values() if f.get("status") == "present"]
        base = (sum(confs) / len(confs)) if confs else 0.3
        st = audit.get("stats", {})
        ratio = st.get("verificadas", 0) / st["evidencias"] if st.get("evidencias") else 0.5
        confidence = round(max(0.05, min(0.95, base * (0.5 + 0.5 * ratio) * (0.75 if verdict == Verdict.REQUIERE_REVISION and (missing_docs or unreadable) else 1.0))), 2)

        ev: list[Evidence] = []
        for r in incumplidos + cumplidos:
            ev.extend(r.evidence[:1])
        summary = (f"Cumplidos: {[r.description for r in cumplidos]}\nIncumplidos: {[r.description + ' -> ' + r.justification for r in incumplidos]}\n"
                   f"Pendientes: {[r.description + ' -> ' + r.justification for r in pendientes]}\nFaltantes: {missing_docs}\nIlegibles: {unreadable}\n"
                   f"Inconsistencias: {[f.description for f in incons]}\nEvidencias verificadas: {st.get('verificadas', 0)} de {st.get('evidencias', 0)}")
        dest = {"store": "Tienda comercial", "cedis": "CEDIS"}.get(getattr(project_type, "value", None), "No especificado")
        explanation, used = reason, False
        try:
            explanation = self.invoke_llm(NarrativeOut, self.human, verdict=verdict.value, dest=dest, summary=summary).explanation
            used = True
        except Exception:
            pass
        rec = FinalRecommendation(verdict=verdict, confidence=confidence, explanation=explanation, reason=reason,
                                  fulfilled=[r.description for r in cumplidos], unfulfilled=[f"{r.description}: {r.justification}" for r in incumplidos],
                                  missing_documents=missing_docs, inconsistencies=[f.description for f in incons],
                                  pending=[f"{r.description}: {r.justification}" for r in pendientes] + [f"Ilegible: {u}" for u in unreadable], evidence=ev[:8])
        self.record(t0, started, "ok", f"{len(requirements)} requisitos", f"{verdict.value} ({confidence})", used_llm=used)
        return rec
