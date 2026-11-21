"""Agente 1 - Coordinador. Orquesta el grafo LangGraph: OCR -> A2 -> (A3|A4|A5|A6 en paralelo) -> (A7 || A8) -> A9 -> A10 -> consolidacion.

Cada nodo ejecuta agentes reales; el coordinador decide que extractores correr segun el contenido clasificado,
paraleliza por documento y conserva el registro de ejecucion y los tiempos de cada etapa."""
from __future__ import annotations

import operator
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Callable, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from agents.auditor import AuditorAgent
from agents.base import BaseAgent
from agents.clasificador import ClasificadorAgent
from agents.comparador import ComparadorAgent
from agents.evaluador import EvaluadorAgent
from agents.extractor_alineamiento import ExtractorAlineamientoAgent
from agents.extractor_escrituras import ExtractorEscriturasAgent
from agents.extractor_predial import ExtractorPredialAgent
from agents.extractor_uso_suelo import ExtractorUsoSueloAgent
from agents.validador import ValidadorAgent
from schemas.schemas import DOC_TYPE_LABELS, DocType, ProcessedDocument, ProjectType
from services.document_processor import process_document
from services.rules_service import LoadedRules
from settings import Settings, get_settings

EXTRACTORS = {"escrituras": ExtractorEscriturasAgent, "predial": ExtractorPredialAgent,
              "alineamiento_numero_oficial": ExtractorAlineamientoAgent, "uso_suelo": ExtractorUsoSueloAgent}


def _merge(a: dict, b: dict) -> dict:
    return {**a, **b}


class State(TypedDict, total=False):
    files: list
    project_type: Optional[ProjectType]
    rules: LoadedRules
    docs: dict
    processed_png: dict
    classifications: list
    extractions: list
    validation: dict
    comparison: dict
    audit: dict
    recommendation: object
    agent_log: Annotated[list, operator.add]
    timings: Annotated[dict, _merge]


class CoordinadorAgent(BaseAgent):
    agent_id = "A1"
    name = "Coordinador"
    responsibility = "Recibir el expediente, organizar documentos, distribuir tareas, paralelizar, gestionar errores y consolidar resultados."
    system_prompt = "Agente de orquestacion (LangGraph): enruta por contenido clasificado; no emite juicios legales."
    uses_llm = False

    def __init__(self, settings: Optional[Settings] = None, progress: Optional[Callable[[str, str], None]] = None):
        super().__init__(settings)
        self.progress = progress or (lambda stage, detail="": None)
        self.graph = self._build()

    # ------------------------------------------------------------------ nodos
    def _stage(self, name: str, detail: str = ""):
        self.progress(name, detail)

    def n_ingest(self, st: State) -> dict:
        t0 = time.monotonic()
        self._stage("Leyendo documentos", f"{len(st['files'])} archivo(s)")
        deadline = time.monotonic() + 240
        docs, pngs = {}, {}

        def work(f):
            doc, png = process_document(f["file_id"], f["name"], f["data"], f["fmt"], self.settings, keep_processed=True, deadline=deadline)
            return f["file_id"], doc, png

        with ThreadPoolExecutor(max_workers=2) as ex:
            for fid, doc, png in ex.map(work, st["files"]):
                docs[fid], pngs[fid] = doc, png
        return {"docs": docs, "processed_png": pngs, "timings": {"lectura_ocr": int((time.monotonic() - t0) * 1000)},
                "agent_log": []}

    def n_classify(self, st: State) -> dict:
        t0 = time.monotonic()
        self._stage("Clasificando documentos por contenido", "Agente 2")
        agents = {fid: ClasificadorAgent(self.settings) for fid in st["docs"]}
        with ThreadPoolExecutor(max_workers=3) as ex:
            results = list(ex.map(lambda fid: agents[fid].run(st["docs"][fid]), st["docs"]))
        log = [e for a in agents.values() for e in a.log]
        return {"classifications": results, "agent_log": log, "timings": {"clasificacion": int((time.monotonic() - t0) * 1000)}}

    def n_extract(self, st: State) -> dict:
        t0 = time.monotonic()
        self._stage("Extrayendo datos con agentes especializados", "Agentes 3, 4, 5 y 6 en paralelo")
        jobs = []
        for c in st["classifications"]:
            dt = c["doc_type"].value
            if dt in EXTRACTORS:
                jobs.append((EXTRACTORS[dt](self.settings), st["docs"][c["file_id"]]))
        def run(job):
            agent, doc = job
            return agent.run(doc, project_type=st.get("project_type"), rules=st["rules"]).model_dump()
        with ThreadPoolExecutor(max_workers=3) as ex:
            results = list(ex.map(run, jobs))
        return {"extractions": results, "agent_log": [e for a, _ in jobs for e in a.log], "timings": {"extraccion": int((time.monotonic() - t0) * 1000)}}

    def n_validate(self, st: State) -> dict:
        t0 = time.monotonic()
        self._stage("Validando requisitos con las reglas configuradas", "Agente 7")
        a = ValidadorAgent(self.settings)
        present = {c["doc_type"].value for c in st["classifications"] if c["doc_type"] != DocType.DESCONOCIDO}
        out = a.run(st["extractions"], present, st.get("project_type"), st["rules"])
        return {"validation": out, "agent_log": a.log, "timings": {"validacion": int((time.monotonic() - t0) * 1000)}}

    def n_compare(self, st: State) -> dict:
        t0 = time.monotonic()
        self._stage("Comparando documentos entre sí", "Agente 8")
        a = ComparadorAgent(self.settings)
        out = a.run(st["extractions"], st["rules"])
        return {"comparison": out, "agent_log": a.log, "timings": {"comparacion": int((time.monotonic() - t0) * 1000)}}

    def n_audit(self, st: State) -> dict:
        t0 = time.monotonic()
        self._stage("Auditando evidencias contra los originales", "Agente 9")
        a = AuditorAgent(self.settings)
        findings = list(st["validation"]["findings"]) + list(st["comparison"]["findings"])
        out = a.run(st["extractions"], findings, st["validation"]["requirements"], st["docs"])
        out["all_findings"] = findings
        return {"audit": out, "agent_log": a.log, "timings": {"auditoria": int((time.monotonic() - t0) * 1000)}}

    def n_evaluate(self, st: State) -> dict:
        t0 = time.monotonic()
        self._stage("Generando la recomendación inicial", "Agente 10")
        a = EvaluadorAgent(self.settings)
        required = st["rules"].rules.documents.required
        present = {c["doc_type"].value for c in st["classifications"] if c["doc_type"] != DocType.DESCONOCIDO}
        missing = [DOC_TYPE_LABELS[DocType(d)] for d in required if d not in present]
        unreadable = [f"{d.filename} (páginas {', '.join(map(str, d.unreadable_pages))})" for d in st["docs"].values() if d.unreadable_pages]
        unreadable += [f"{d.filename} (sin texto legible)" for d in st["docs"].values() if not d.has_text and not d.unreadable_pages]
        rec = a.run(st["validation"]["requirements"], st["audit"]["all_findings"], st["comparison"]["comparisons"], st["audit"],
                    st["extractions"], missing, unreadable, st.get("project_type"), st["rules"])
        return {"recommendation": rec, "agent_log": a.log, "timings": {"recomendacion": int((time.monotonic() - t0) * 1000)}}

    # ------------------------------------------------------------------ grafo
    def _build(self):
        g = StateGraph(State)
        for name, fn in (("ingest", self.n_ingest), ("classify", self.n_classify), ("extract", self.n_extract), ("validate", self.n_validate),
                         ("compare", self.n_compare), ("audit", self.n_audit), ("evaluate", self.n_evaluate)):
            g.add_node(name, fn)
        g.add_edge(START, "ingest")
        g.add_edge("ingest", "classify")
        g.add_edge("classify", "extract")
        g.add_edge("extract", "validate")   # A7 y A8 corren en paralelo
        g.add_edge("extract", "compare")
        g.add_edge(["validate", "compare"], "audit")
        g.add_edge("audit", "evaluate")
        g.add_edge("evaluate", END)
        return g.compile()

    # ---------------------------------------------------------------- consolidacion
    def analyze(self, exp_id: str, files: list[dict], project_type: Optional[ProjectType], rules: LoadedRules) -> tuple[dict, dict]:
        """files: [{file_id, name, fmt, data}]. Devuelve (resultado_serializable, imagenes_procesadas)."""
        t0, started = time.monotonic(), self.now()
        self._stage("Expediente recibido", exp_id)
        st = self.graph.invoke({"files": files, "project_type": project_type, "rules": rules, "agent_log": [], "timings": {}})
        total = int((time.monotonic() - t0) * 1000)
        by_id = {f["file_id"]: f for f in files}
        docs_out = []
        for c in st["classifications"]:
            d: ProcessedDocument = st["docs"][c["file_id"]]
            docs_out.append({"file_id": c["file_id"], "filename": d.filename, "doc_type": c["doc_type"].value, "classification_confidence": c["confidence"],
                             "classification_reason": c["reasoning"], "pages_total": d.pages_total, "ocr_engine": d.ocr_engine, "elapsed_ms": d.elapsed_ms,
                             "pages": [{"page": p.page, "status": p.status, "source": p.source, "ocr_confidence": p.ocr_confidence, "issues": p.issues} for p in d.pages],
                             "unreadable_pages": d.unreadable_pages, "has_text": d.has_text})
        entry = self.record(t0, started, "ok", f"{len(files)} archivos", f"{st['recommendation'].verdict.value} en {total/1000:.1f}s", used_llm=False)
        log = [e.model_dump(mode="json") for e in st["agent_log"] + [entry]]
        result = {
            "expediente_id": exp_id, "project_type": project_type.value if project_type else None, "rules_reference": st["rules"].reference(),
            "documents": docs_out, "extractions": st["extractions"],
            "requirements": [r.model_dump(mode="json") for r in st["validation"]["requirements"]],
            "findings": [f.model_dump(mode="json") for f in st["audit"]["all_findings"]], "comparisons": st["comparison"]["comparisons"],
            "audit": {k: v for k, v in st["audit"].items() if k != "all_findings"}, "recommendation": st["recommendation"].model_dump(mode="json"),
            "agent_log": log, "timings_ms": {**st["timings"], "total": total},
        }
        return result, st["processed_png"]
