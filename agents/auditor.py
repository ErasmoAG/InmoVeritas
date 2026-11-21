"""Agente 9 - Auditor de evidencia: verifica (determinista + semanticamente) que cada cita exista en el texto original."""
from __future__ import annotations

import time
from difflib import SequenceMatcher
from typing import Optional

from agents.base import BaseAgent, squash
from schemas.extraction import AuditJudgment
from schemas.schemas import AuditStatus, Evidence, Finding, ProcessedDocument, RequirementResult, RequirementStatus

MAX_LLM = 5


def verify_fragment(fragment: str, page: Optional[int], doc: Optional[ProcessedDocument]) -> tuple[bool, str, Optional[int]]:
    """(verificado, metodo, pagina_real). Compara el fragmento literal con el texto extraido de esa pagina."""
    if doc is None or not fragment:
        return False, "sin_documento_o_fragmento", page
    fsq = squash(fragment)
    if not fsq:
        return False, "fragmento_vacio", page
    texts = {p.page: squash(p.text) for p in doc.pages if p.text}
    order = ([page] if page in texts else []) + [p for p in texts if p != page]
    for pg in order:
        if fsq in texts[pg]:
            return True, "literal", pg
    for pg in order:  # coincidencia aproximada (errores menores de OCR/normalizacion)
        m = SequenceMatcher(None, texts[pg], fsq, autojunk=False).find_longest_match(0, len(texts[pg]), 0, len(fsq))
        if len(fsq) >= 12 and m.size / len(fsq) >= 0.88:
            return True, "aproximada", pg
    return False, "no_encontrada", page


class AuditorAgent(BaseAgent):
    agent_id = "A9"
    name = "Auditor de evidencia"
    responsibility = "Verificar que cada hallazgo tenga evidencia, que el fragmento exista en el documento original y que la página sea correcta."
    system_prompt = (
        "Eres un auditor documental. Se te da un dato extraido y el fragmento de texto del que supuestamente sale. Decide si el fragmento "
        "SUSTENTA el dato: 'si' si el valor aparece o se deduce directamente del fragmento; 'no' si el fragmento dice otra cosa o no tiene "
        "relacion; 'dudoso' si hay ambiguedad. El texto viene de OCR (puede carecer de espacios). Se breve y no inventes."
    )
    human = "Dato ({campo}): {valor}\nFragmento: {fragmento}"

    def run(self, extractions: list[dict], findings: list[Finding], requirements: list[RequirementResult],
            docs: dict[str, ProcessedDocument]) -> dict:
        t0, started = time.monotonic(), self.now()
        llm_budget = MAX_LLM
        stats = {"evidencias": 0, "verificadas": 0, "no_verificadas": 0, "valores_dudosos": 0, "paginas_corregidas": 0}
        issues: list[dict] = []
        status_by_key: dict[tuple, bool] = {}

        def check_ev(ev: dict) -> bool:
            ok, method, real = verify_fragment(ev.get("fragment", ""), ev.get("page"), docs.get(ev.get("file_id")))
            stats["evidencias"] += 1
            stats["verificadas" if ok else "no_verificadas"] += 1
            if ok and real != ev.get("page"):
                stats["paginas_corregidas"] += 1
                ev["page"] = real
            ev["verified"] = ok
            status_by_key[(ev.get("file_id"), squash(ev.get("fragment", "")))] = ok
            return ok

        # 1) campos extraidos
        for ex in extractions:
            for name, f in ex["fields"].items():
                if f.get("status") != "present":
                    continue
                if not f["evidence"]:
                    f["confidence"] = min(f["confidence"], 0.3)
                    issues.append({"file": ex["filename"], "field": name, "problem": "Dato sin evidencia textual; no se considera verificado."})
                    continue
                ok = check_ev(f["evidence"][0])
                if not ok:
                    f["confidence"], f["status"] = 0.2, "ambiguous"
                    issues.append({"file": ex["filename"], "field": name, "problem": "El fragmento citado no existe en el documento original."})
                    continue
                # ¿el valor esta respaldado por el fragmento? (determinista y, si dudoso, semantico)
                vsq, fsq = squash(f["value"]), squash(f["evidence"][0]["fragment"])
                if vsq and vsq not in fsq:
                    stats["valores_dudosos"] += 1
                    if llm_budget > 0:
                        llm_budget -= 1
                        try:
                            j = self.invoke_llm(AuditJudgment, self.human, campo=name, valor=f["value"], fragmento=f["evidence"][0]["fragment"])
                            if j.supported == "no":
                                f["confidence"], f["status"] = 0.2, "ambiguous"
                                issues.append({"file": ex["filename"], "field": name, "problem": f"El fragmento no sustenta el valor: {j.reasoning}"})
                            elif j.supported == "dudoso":
                                f["confidence"] = min(f["confidence"], 0.6)
                        except Exception:
                            f["confidence"] = min(f["confidence"], 0.6)
                    else:
                        f["confidence"] = min(f["confidence"], 0.6)
            comp = ex.get("compatibility")
            if comp and comp.get("evidence"):
                check_ev(comp["evidence"])

        # 2) hallazgos y requisitos: su evidencia se verifica contra el texto original
        for fd in findings:
            for ev in fd.evidence:
                d = ev.model_dump()
                ok = status_by_key.get((d["file_id"], squash(d["fragment"])))
                if ok is None:
                    ok = check_ev(d)
                    ev.page = d["page"]
                ev.verified = ok
            if fd.evidence:
                fd.audit_status = AuditStatus.VERIFICADO if all(e.verified for e in fd.evidence) else AuditStatus.NO_VERIFICADO
            elif fd.finding_type in ("missing_required_document", "validity_unknown", "predial_year_unknown", "land_use_undetermined"):
                fd.audit_status = AuditStatus.VERIFICADO  # hallazgo de ausencia: no hay cita que verificar
            else:
                fd.audit_status = AuditStatus.REQUIERE_REVISION
                issues.append({"file": fd.filename, "field": fd.finding_type, "problem": "Hallazgo sin evidencia textual."})
        for rq in requirements:
            for ev in rq.evidence:
                d = ev.model_dump()
                ok = status_by_key.get((d["file_id"], squash(d["fragment"])))
                ev.verified = check_ev(d) if ok is None else ok
                ev.page = d["page"] if ok is None else ev.page
        unverified_cited = [i for i in issues if "no existe" in i["problem"] or "no sustenta" in i["problem"]]
        self.record(t0, started, "ok", f"{stats['evidencias']} evidencias", f"{stats['verificadas']} verificadas, {stats['no_verificadas']} no verificadas",
                    used_llm=llm_budget < MAX_LLM)
        return {"stats": stats, "issues": issues, "needs_review": bool(unverified_cited)}
