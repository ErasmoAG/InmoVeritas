"""Pruebas de los agentes deterministas (7, 8, 9, 10) con extracciones sinteticas: no requieren Ollama."""
from datetime import date

from agents.auditor import AuditorAgent, verify_fragment
from agents.comparador import ComparadorAgent
from agents.common import parse_date, parse_surface, parse_year
from agents.evaluador import EvaluadorAgent
from agents.validador import ValidadorAgent
from schemas.schemas import AuditStatus, PageContent, ProcessedDocument, ProjectType, Verdict
from services.rules_service import load_rules

RULES = load_rules()


def field(value, page=1, fragment=None, conf=0.9, file_id="f1", filename="doc.pdf"):
    return {"value": value, "status": "present", "confidence": conf,
            "evidence": [{"file_id": file_id, "filename": filename, "page": page, "fragment": fragment or str(value), "verified": None}]}


def ex(doc_type, fid, fields, compat=None):
    return {"file_id": fid, "filename": f"{doc_type}.pdf", "doc_type": doc_type, "fields": fields, "compatibility": compat, "status": "ok", "legibility_notes": []}


def doc(fid, text, name="x.pdf"):
    return ProcessedDocument(file_id=fid, filename=name, pages_total=1, pages=[PageContent(page=1, text=text, source="ocr", status="ok")])


ALL = {"escrituras", "predial", "alineamiento_numero_oficial", "uso_suelo"}


def comp(verdict, frag="USO HABITACIONAL"):
    return {"verdict": verdict, "reasoning": "razon", "evidence": {"file_id": "u", "filename": "u.pdf", "page": 1, "fragment": frag, "verified": None}}


# ----------------------------------------------------------------- utilidades
def test_parseo_de_fechas_anios_y_superficies():
    assert parse_date("VIGENCIA:03DEMARZODE2026") == date(2026, 3, 3)
    assert parse_date("26/01/2025 12:21") == date(2025, 1, 26)
    assert parse_date("sin fecha") is None
    assert parse_year("EJERCICIO 2019") == 2019 and parse_year("nada") is None
    assert parse_surface("1,250.50 m2") == 1250.5 and parse_surface(">572.75 m2") == 572.75 and parse_surface("s/n") is None


# ----------------------------------------------------------------- A7 validador
def test_a7_predial_anterior_a_2020_es_hallazgo_alto():
    ext = [ex("predial", "p", {"fiscal_year": field("2019", file_id="p")})]
    out = ValidadorAgent().run(ext, {"predial"}, ProjectType.STORE, RULES, today=date(2026, 1, 1))
    f = [x for x in out["findings"] if x.finding_type == "predial_year_below_min"]
    assert len(f) == 1 and f[0].severity.value == "high" and f[0].evidence and f[0].found_value == "2019"


def test_a7_predial_2025_cumple_y_anio_desconocido_queda_pendiente():
    ok = ValidadorAgent().run([ex("predial", "p", {"fiscal_year": field("2025", file_id="p")})], {"predial"}, ProjectType.STORE, RULES)
    assert any(r.requirement_id.startswith("PREDIAL-year") and r.status.value == "cumplido" for r in ok["requirements"])
    unk = ValidadorAgent().run([ex("predial", "p", {})], {"predial"}, ProjectType.STORE, RULES)
    assert any(f.finding_type == "predial_year_unknown" for f in unk["findings"])
    assert not any(f.finding_type == "predial_year_below_min" for f in unk["findings"])  # desconocido != incumplimiento


def test_a7_documentos_faltantes_y_vigencias():
    out = ValidadorAgent().run([], {"predial"}, ProjectType.CEDIS, RULES)
    missing = [f for f in out["findings"] if f.finding_type == "missing_required_document"]
    assert len(missing) == 3
    ext = [ex("uso_suelo", "u", {"validity": field("03 de marzo de 2020", file_id="u")}, comp("compatible"))]
    out = ValidadorAgent().run(ext, {"uso_suelo"}, ProjectType.CEDIS, RULES, today=date(2026, 1, 1))
    assert any(f.finding_type == "document_expired" for f in out["findings"])


def test_a7_uso_de_suelo_depende_del_destino():
    inc = [ex("uso_suelo", "u", {"authorized_use": field("Habitacional", file_id="u")}, comp("incompatible"))]
    store = ValidadorAgent().run(inc, {"uso_suelo"}, ProjectType.STORE, RULES)
    assert any(f.finding_type == "land_use_incompatible" for f in store["findings"])
    assert all("provisional" in (r.rule_level or "") for r in store["requirements"] if r.requirement_id == "USO-compat")
    nodest = ValidadorAgent().run(inc, {"uso_suelo"}, None, RULES)   # sin destino: pendiente, nunca incumplimiento
    assert any(f.finding_type == "land_use_undetermined" for f in nodest["findings"])
    assert not any(f.finding_type == "land_use_incompatible" for f in nodest["findings"])


# ----------------------------------------------------------------- A8 comparador
def test_a8_superficies_distintas_sin_tolerancia_oficial_se_reporta_para_revision():
    exts = [ex("escrituras", "e", {"land_surface_m2": field("500 m2", file_id="e")}), ex("predial", "p", {"registered_surface_m2": field("450.00", file_id="p")})]
    out = ComparadorAgent().run(exts, RULES)
    f = [x for x in out["findings"] if x.finding_type == "inconsistency"]
    assert len(f) == 1 and "500" in f[0].found_value and "450" in f[0].compared_value and "tolerancia" in f[0].description
    assert f[0].evidence and f[0].severity.value == "medium"


def test_a8_misma_clave_y_nombres_reordenados_no_son_inconsistencia():
    exts = [ex("escrituras", "e", {"property_id": field("1120314518000000", file_id="e"), "owner": field("QUINTANAR LEDESMA ROSA MARIA", file_id="e")}),
            ex("predial", "p", {"predial_account": field("clave 1120-3145-18000000", file_id="p"), "owner": field("Rosa Maria Quintanar Ledesma", file_id="p")})]
    out = ComparadorAgent().run(exts, RULES)
    assert out["findings"] == []
    assert {c["field"]: c["result"] for c in out["comparisons"]}["owner"] == "igual"


def test_a8_clave_distinta_es_inconsistencia_de_alta_gravedad():
    exts = [ex("escrituras", "e", {"property_id": field("1120314518000000", file_id="e")}), ex("predial", "p", {"predial_account": field("0070151126000000", file_id="p")})]
    f = ComparadorAgent().run(exts, RULES)["findings"]
    assert len(f) == 1 and f[0].severity.value == "high"


# ----------------------------------------------------------------- A9 auditor
def test_a9_verifica_citas_reales_y_detecta_citas_inventadas():
    d = doc("f1", "PROPIETARIO/POSEEDOR QUINTANAR LEDESMA ROSA MARIA\nSUPERFICIE TERRENO 245.00")
    assert verify_fragment("PROPIETARIO/POSEEDOR QUINTANAR LEDESMA ROSA MARIA", 1, d)[:2] == (True, "literal")
    assert verify_fragment("PROPIETARIO JUAN PEREZ INVENTADO", 1, d)[0] is False
    ok, method, page = verify_fragment("SUPERFICIE TERRENO 245.00", 3, d)   # pagina equivocada: se corrige
    assert ok and page == 1


def test_a9_marca_campos_con_evidencia_falsa_y_baja_su_confianza():
    d = doc("f1", "SUPERFICIE TERRENO 245.00")
    ext = [ex("predial", "f1", {"registered_surface_m2": field("245.00", fragment="SUPERFICIE TERRENO 245.00"),
                                "owner": field("JUAN PEREZ", fragment="PROPIETARIO JUAN PEREZ LOPEZ")})]
    out = AuditorAgent().run(ext, [], [], {"f1": d})
    f = ext[0]["fields"]
    assert f["registered_surface_m2"]["evidence"][0]["verified"] is True
    assert f["owner"]["evidence"][0]["verified"] is False and f["owner"]["confidence"] <= 0.2
    assert out["stats"]["verificadas"] == 1 and out["stats"]["no_verificadas"] == 1 and out["needs_review"]


# ----------------------------------------------------------------- A10 evaluador
def run_pipeline_det(extractions, present, project, docs):
    v = ValidadorAgent().run(extractions, present, project, RULES, today=date(2026, 6, 1))
    c = ComparadorAgent().run(extractions, RULES)
    findings = v["findings"] + c["findings"]
    audit = AuditorAgent().run(extractions, findings, v["requirements"], docs)
    missing = [m for m in ("Escrituras", "Predial", "Alineamiento y Número Oficial", "Uso de Suelo") if m.split()[0].lower() not in " ".join(sorted(present)).replace("_", " ")]
    rec = EvaluadorAgent().run(v["requirements"], findings, c["comparisons"], audit, extractions, [] if len(present) == 4 else ["faltan"], [], project, RULES)
    return rec


def test_a10_no_cumple_requiere_evidencia_verificada_y_cumple_solo_sin_pendientes():
    t = "ejercicio fiscal 2019 USO HABITACIONAL NO SE PERMITEN USOS COMERCIALES"
    docs = {"p": doc("p", t, "p.pdf"), "u": doc("u", t, "u.pdf")}
    base = {"fiscal_year": field("2019", fragment="ejercicio fiscal 2019", file_id="p", filename="p.pdf")}
    ext = [ex("predial", "p", base), ex("uso_suelo", "u", {"authorized_use": field("Habitacional", fragment="USO HABITACIONAL", file_id="u")}, comp("incompatible", "USO HABITACIONAL"))]
    ext[1]["compatibility"]["evidence"]["file_id"] = "u"
    rec = run_pipeline_det(ext, {"predial", "uso_suelo"}, ProjectType.STORE, docs)
    assert rec.verdict in (Verdict.NO_CUMPLE, Verdict.REQUIERE_REVISION) and rec.disclaimer
    # una cita inventada impide el veredicto "No cumple": queda "Requiere revision"
    ext2 = [ex("predial", "p", {"fiscal_year": field("2019", fragment="TEXTO QUE NO EXISTE EN EL DOCUMENTO", file_id="p", filename="p.pdf")})]
    rec2 = run_pipeline_det(ext2, {"predial"}, ProjectType.STORE, docs)
    assert rec2.verdict == Verdict.REQUIERE_REVISION
