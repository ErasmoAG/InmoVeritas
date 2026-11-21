"""Agente 7 - Validador de requisitos y reglas: evalua SOLO con el archivo externo de reglas (YAML)."""
from __future__ import annotations

import time
from datetime import date
from typing import Optional

from agents.base import BaseAgent
from agents.common import get_field, parse_date, parse_year
from schemas.schemas import (AuditStatus, DOC_TYPE_LABELS, DocType, Evidence, Finding, ProjectType, RequirementResult,
                             RequirementStatus, Severity)
from services.rules_service import LoadedRules


def _ev(field: Optional[dict]) -> list[Evidence]:
    return [Evidence(**e) for e in (field or {}).get("evidence", [])]


class ValidadorAgent(BaseAgent):
    agent_id = "A7"
    name = "Validador de requisitos y reglas"
    responsibility = "Evaluar requisitos, vigencias, año del predial y uso de suelo con las reglas configurables; justificar con evidencia."
    system_prompt = "Agente determinista: aplica rules_config.yaml sin inventar reglas juridicas."
    uses_llm = False

    def run(self, extractions: list[dict], doc_types_present: set[str], project_type: Optional[ProjectType],
            rules: LoadedRules, today: Optional[date] = None) -> dict:
        t0, started = time.monotonic(), self.now()
        today = today or date.today()
        r = rules.rules
        reqs: list[RequirementResult] = []
        findings: list[Finding] = []
        n = [0]

        def fid() -> str:
            n[0] += 1
            return f"V-{n[0]:02d}"

        def sev(kind: str) -> Severity:
            return Severity(r.finding_classification.get(kind, "medium"))

        # ---- documentos requeridos
        for dt in r.documents.required:
            label = DOC_TYPE_LABELS[DocType(dt)]
            if dt in doc_types_present:
                reqs.append(RequirementResult(requirement_id=f"DOC-{dt}", description=f"Documento recibido: {label}",
                                              status=RequirementStatus.CUMPLIDO, justification=f"Se recibió y clasificó un documento de tipo {label}.",
                                              rule="documents.required", rule_level=r.documents.level))
            else:
                st = RequirementStatus.INCUMPLIDO if r.documents.missing_document_outcome == "not_compliant" else RequirementStatus.PENDIENTE
                reqs.append(RequirementResult(requirement_id=f"DOC-{dt}", description=f"Documento faltante: {label}", status=st,
                                              justification=f"No se recibió ningún documento clasificado como {label}.", rule="documents.required", rule_level=r.documents.level))
                findings.append(Finding(finding_id=fid(), description=f"Falta el documento requerido: {label}.", finding_type="missing_required_document",
                                        severity=sev("missing_required_document"), rule="documents.required", agent=self.agent_id, confidence=1.0))

        # ---- predial: ejercicio fiscal
        if r.predial.enabled:
            hits = get_field(extractions, "predial", "fiscal_year")
            if "predial" in doc_types_present:
                if not hits:
                    reqs.append(RequirementResult(requirement_id="PREDIAL-year", description=f"Predial del ejercicio {r.predial.min_year} o posterior",
                                                  status=RequirementStatus.PENDIENTE, justification="No se pudo identificar el ejercicio fiscal del predial; requiere verificación.",
                                                  rule="predial.min_year", rule_level=r.predial.level))
                    findings.append(Finding(finding_id=fid(), description="No se identificó el año del predial; pendiente de verificación.", finding_type="predial_year_unknown",
                                            severity=sev("predial_year_unknown"), rule="predial.min_year", agent=self.agent_id, confidence=0.5))
                for ex, f in hits:
                    year = parse_year(f["value"])
                    ev = _ev(f)
                    if year is None:
                        reqs.append(RequirementResult(requirement_id=f"PREDIAL-year-{ex['filename'][:20]}", description="Ejercicio fiscal del predial", status=RequirementStatus.PENDIENTE,
                                                      justification=f"El valor extraído («{f['value']}») no permite determinar el año.", rule="predial.min_year", rule_level=r.predial.level, evidence=ev))
                    elif r.predial.min_year and year < r.predial.min_year:
                        reqs.append(RequirementResult(requirement_id=f"PREDIAL-year-{ex['filename'][:20]}", description=f"Predial del ejercicio {r.predial.min_year} o posterior", status=RequirementStatus.INCUMPLIDO,
                                                      justification=f"El predial corresponde al ejercicio {year}, anterior al mínimo configurado ({r.predial.min_year}).", rule="predial.min_year", rule_level=r.predial.level, evidence=ev))
                        findings.append(Finding(finding_id=fid(), description=f"El predial es del ejercicio {year}, anterior a {r.predial.min_year}.", finding_type="predial_year_below_min", severity=sev("predial_year_below_min"),
                                                file_id=ex["file_id"], filename=ex["filename"], page=ev[0].page if ev else None, fragment=ev[0].fragment if ev else None, found_value=str(year),
                                                compared_value=f"mínimo {r.predial.min_year}", rule="predial.min_year", agent=self.agent_id, confidence=f["confidence"], evidence=ev))
                    else:
                        reqs.append(RequirementResult(requirement_id=f"PREDIAL-year-{ex['filename'][:20]}", description=f"Predial del ejercicio {r.predial.min_year} o posterior", status=RequirementStatus.CUMPLIDO,
                                                      justification=f"El predial corresponde al ejercicio {year} (mínimo {r.predial.min_year}).", rule="predial.min_year", rule_level=r.predial.level, evidence=ev))

        # ---- vigencia (solo si el documento la declara)
        if r.validity.enabled:
            for dt in r.validity.documents_with_validity:
                if dt not in doc_types_present:
                    continue
                label = DOC_TYPE_LABELS[DocType(dt)]
                hits = get_field(extractions, dt, "validity")
                if not hits:
                    reqs.append(RequirementResult(requirement_id=f"VIG-{dt}", description=f"Vigencia: {label}", status=RequirementStatus.PENDIENTE,
                                                  justification="El documento no declara una vigencia identificable; no se asume ninguna duración.", rule="validity", rule_level=r.validity.level))
                    findings.append(Finding(finding_id=fid(), description=f"{label}: no se identificó vigencia.", finding_type="validity_unknown", severity=sev("validity_unknown"), rule="validity", agent=self.agent_id, confidence=0.5))
                for ex, f in hits:
                    d, ev = parse_date(f["value"]), _ev(f)
                    if d is None:
                        reqs.append(RequirementResult(requirement_id=f"VIG-{dt}", description=f"Vigencia: {label}", status=RequirementStatus.PENDIENTE, justification=f"No se pudo interpretar la vigencia «{f['value']}».", rule="validity", rule_level=r.validity.level, evidence=ev))
                    elif d < today:
                        reqs.append(RequirementResult(requirement_id=f"VIG-{dt}", description=f"Vigencia: {label}", status=RequirementStatus.INCUMPLIDO, justification=f"La vigencia declarada ({d.isoformat()}) ya venció.", rule="validity", rule_level=r.validity.level, evidence=ev))
                        findings.append(Finding(finding_id=fid(), description=f"{label}: vigencia vencida ({d.isoformat()}).", finding_type="document_expired", severity=sev("document_expired"), file_id=ex["file_id"], filename=ex["filename"],
                                                page=ev[0].page if ev else None, fragment=ev[0].fragment if ev else None, found_value=d.isoformat(), compared_value=f"hoy {today.isoformat()}", rule="validity", agent=self.agent_id, confidence=f["confidence"], evidence=ev))
                    else:
                        reqs.append(RequirementResult(requirement_id=f"VIG-{dt}", description=f"Vigencia: {label}", status=RequirementStatus.CUMPLIDO, justification=f"Vigente hasta {d.isoformat()}.", rule="validity", rule_level=r.validity.level, evidence=ev))

        # ---- uso de suelo según destino
        if "uso_suelo" in doc_types_present:
            dest = {"store": "tienda comercial", "cedis": "CEDIS"}.get(getattr(project_type, "value", None))
            comp_rule = getattr(r.land_use, project_type.value) if project_type else None
            for ex in [e for e in extractions if e["doc_type"] == "uso_suelo"]:
                comp = ex.get("compatibility")
                ev = [Evidence(**comp["evidence"])] if comp and comp.get("evidence") else []
                if not dest or not comp:
                    reqs.append(RequirementResult(requirement_id="USO-compat", description="Compatibilidad del uso de suelo con el destino", status=RequirementStatus.PENDIENTE,
                                                  justification="No se conoce el destino (tienda o CEDIS) o no se pudo evaluar el documento." if not dest else "No se pudo evaluar la compatibilidad.", rule="land_use", rule_level="provisional_mvp"))
                    findings.append(Finding(finding_id=fid(), description="Compatibilidad de uso de suelo pendiente de evaluar.", finding_type="land_use_undetermined", severity=sev("land_use_undetermined"), rule="land_use", agent=self.agent_id, confidence=0.4))
                    continue
                lvl = comp_rule.level if comp_rule else "provisional_mvp"
                note = " (criterio provisional del MVP, pendiente de confirmar con el comité)" if comp_rule and comp_rule.requires_committee_confirmation else ""
                v = comp["verdict"]
                if v == "compatible":
                    reqs.append(RequirementResult(requirement_id="USO-compat", description=f"Uso de suelo compatible con {dest}", status=RequirementStatus.CUMPLIDO, justification=comp["reasoning"] + note, rule=f"land_use.{project_type.value}", rule_level=lvl, evidence=ev))
                elif v == "incompatible":
                    reqs.append(RequirementResult(requirement_id="USO-compat", description=f"Uso de suelo compatible con {dest}", status=RequirementStatus.INCUMPLIDO, justification=comp["reasoning"] + note, rule=f"land_use.{project_type.value}", rule_level=lvl, evidence=ev))
                    findings.append(Finding(finding_id=fid(), description=f"El uso de suelo no es compatible con {dest}: {comp['reasoning']}", finding_type="land_use_incompatible", severity=sev("land_use_incompatible"), file_id=ex["file_id"], filename=ex["filename"],
                                            page=ev[0].page if ev else None, fragment=ev[0].fragment if ev else None, found_value=(get_field([ex], "uso_suelo", "authorized_use") or [(0, {"value": None})])[0][1]["value"], compared_value=dest,
                                            rule=f"land_use.{project_type.value}", agent=self.agent_id, confidence=0.8, evidence=ev))
                else:
                    reqs.append(RequirementResult(requirement_id="USO-compat", description=f"Uso de suelo compatible con {dest}", status=RequirementStatus.PENDIENTE, justification=comp["reasoning"] + note, rule=f"land_use.{project_type.value}", rule_level=lvl, evidence=ev))
                    findings.append(Finding(finding_id=fid(), description=f"No se puede determinar la compatibilidad del uso de suelo con {dest}.", finding_type="land_use_undetermined", severity=sev("land_use_undetermined"), file_id=ex["file_id"], filename=ex["filename"], rule=f"land_use.{project_type.value}", agent=self.agent_id, confidence=0.5, evidence=ev))
        elif project_type is None:
            pass

        self.record(t0, started, "ok", f"{len(extractions)} extracciones", f"{len(reqs)} requisitos, {len(findings)} hallazgos")
        return {"requirements": reqs, "findings": findings}
