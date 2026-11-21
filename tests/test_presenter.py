"""La capa de presentacion explica en lenguaje sencillo SIN alterar resultados, evidencias ni veredicto."""
import copy
import json
import re

from services import presenter

JARGON = ["discrepancia", "interdocumental", "inconsistencia", "validación cruzada", "extracción", "entidades", "metadatos", "evidencia documental",
          "divergencia", "anomalía", "parámetro", "semántic", "ocr", "atributo", "incongruencia", "estructurad", "clasificación", "ejercicio fiscal"]


def doc(fid, dt, name=None, pages=1, unreadable=(), has_text=True, low=()):
    pg = [{"page": i + 1, "status": "ilegible" if (i + 1) in unreadable else ("baja_calidad" if (i + 1) in low else "ok"), "source": "ocr", "ocr_confidence": 0.9, "issues": []} for i in range(pages)]
    return {"file_id": fid, "filename": name or f"{dt}.pdf", "doc_type": dt, "classification_confidence": 0.9, "classification_reason": "", "pages_total": pages, "ocr_engine": "x",
            "elapsed_ms": 1, "pages": pg, "unreadable_pages": list(unreadable), "has_text": has_text}


def ev(fid, name, page, frag, verified=True):
    return {"file_id": fid, "filename": name, "page": page, "fragment": frag, "verified": verified}


def finding(fid, ftype, desc, sev="medium", file_id=None, filename=None, page=None, found=None, compared=None, rule=None, evidence=(), audit="verificado"):
    return {"finding_id": fid, "description": desc, "finding_type": ftype, "severity": sev, "file_id": file_id, "filename": filename, "page": page, "fragment": None,
            "found_value": found, "compared_value": compared, "rule": rule, "agent": "A8", "confidence": 0.8, "audit_status": audit, "evidence": list(evidence)}


def result(docs, findings=(), reqs=(), verdict="requiere_revision", audit_issues=(), confidence=0.8, project="store"):
    return {"expediente_id": "EXP-T", "project_type": project, "documents": docs, "extractions": [], "requirements": list(reqs), "findings": list(findings), "comparisons": [],
            "audit": {"stats": {}, "issues": list(audit_issues)}, "recommendation": {"verdict": verdict, "confidence": confidence, "explanation": "x", "reason": "x", "disclaimer": "No sustituye al abogado."},
            "agent_log": [], "timings_ms": {"total": 1}}


FOUR = [doc("e", "escrituras"), doc("p", "predial"), doc("a", "alineamiento_numero_oficial"), doc("u", "uso_suelo")]


def all_user_text(p) -> str:
    chunks = [p["summary"], p["final_text"]]
    for f in p["findings"]:
        chunks += [f["title"], f["text"], f["why"], f["recommendation"]]
    for m in p["missing"]:
        chunks += [m["text"], m["recommendation"]]
    for d in p["documents"]:
        chunks += d["reasons"] + [d["status_help"]]
    return " ".join(chunks).lower()


def assert_plain(p):
    t = all_user_text(p)
    bad = [w for w in JARGON if w in t]
    assert not bad, f"Tecnicismos en el texto: {bad}\n{t}"


# ------------------------------------------------------------------ escenarios
def test_expediente_que_cumple():
    p = presenter.build(result(FOUR, verdict="cumple"))
    assert p["verdict"] == "cumple" and p["verdict_label"] == "Cumple"
    assert p["findings"] == [] and p["missing"] == []
    assert [c["status"] for c in p["documents"]] == ["correcto"] * 4
    assert "cuatro documentos" in p["summary"] and "No se encontraron problemas" in p["summary"]
    assert_plain(p)


def test_documentos_faltantes_se_explican_y_no_son_incumplimiento():
    p = presenter.build(result([doc("e", "escrituras"), doc("p", "predial")], [finding("V-01", "missing_required_document", "Falta el documento requerido: Uso de Suelo.", "high")]))
    st = {c["type"]: c["status"] for c in p["documents"]}
    assert st["uso_suelo"] == "faltante" and st["alineamiento_numero_oficial"] == "faltante" and st["predial"] == "correcto"
    assert len(p["missing"]) == 2 and "Sin este documento no es posible comprobar" in p["missing"][0]["text"]
    assert "2 de los 4" in p["summary"] and "Falta:" in p["summary"]
    assert p["findings"] == []          # el faltante se muestra en su propia seccion, sin duplicarse
    assert_plain(p)


def test_diferencia_de_superficie_explica_que_documentos_valores_y_pagina():
    f = finding("C-01", "inconsistency", "Superficie (m²): no coincide entre Escrituras y Predial. Diferencia de 50.00 m² (10.0 %). No hay tolerancia oficial configurada: se reporta para revisión, no como incumplimiento.",
                file_id="p", filename="predial.pdf", found="Escrituras: 500 m2", compared="Predial: 450", rule="cross_checks.surface",
                evidence=[ev("e", "escrituras.pdf", 3, "SUPERFICIE 500 M2"), ev("p", "predial.pdf", 1, "SUPERFICIE TERRENO 450")])
    p = presenter.build(result(FOUR, [f]))
    it = p["findings"][0]
    assert it["title"] == "Diferencia en la superficie del inmueble"
    assert "Escrituras indica «500 m2», mientras que Predial indica «450»" in it["text"] and "50.00 m²" in it["text"]
    assert [(d["label"], d["page"]) for d in it["docs"]] == [("Escrituras", 3), ("Predial", 1)]
    assert it["recommendation"] and it["why"] and len(it["evidence"]) == 2 and it["evidence"][0]["fragment"] == "SUPERFICIE 500 M2"
    assert {c["type"]: c["status"] for c in p["documents"]}["escrituras"] == "requiere_revision"
    assert_plain(p)


def test_propietario_direccion_y_clave_con_redaccion_propia():
    mk = lambda rule, a, b: finding("C-01", "inconsistency", "x", found=f"Escrituras: {a}", compared=f"Predial: {b}", rule=f"cross_checks.{rule}", evidence=[ev("e", "e.pdf", 1, a), ev("p", "p.pdf", 1, b)])
    t = {r: presenter.build(result(FOUR, [mk(r, "AAA", "BBB")]))["findings"][0]["title"] for r in ("owner", "address", "parcel_id")}
    assert t["owner"] == "El nombre del propietario no coincide"
    assert t["address"] == "La dirección no coincide entre documentos" and t["parcel_id"] == "La clave del predio no coincide"


def test_imagen_ilegible_no_se_presenta_como_documento_incorrecto():
    docs = [doc("e", "escrituras"), doc("x", "desconocido", "foto_borrosa.jpg", unreadable=(1,), has_text=False)]
    p = presenter.build(result(docs))
    it = next(i for i in p["findings"] if i["type"] == "unreadable")
    assert "no se puede leer" in it["text"] and "más claro" in it["recommendation"] and "borrosa" in it["title"]
    assert any(c["status"] == "ilegible" for c in p["documents"])
    assert "no se pudo leer" in p["summary"]
    assert not any(c["status"] == "no_cumple" for c in p["documents"])
    assert_plain(p)


def test_no_cumple_uso_de_suelo_y_predial_antiguo():
    f1 = finding("V-02", "land_use_incompatible", "El uso de suelo no es compatible con tienda comercial: Solo permite uso habitacional.", "high", file_id="u", filename="u.pdf", page=1,
                 found="Habitacional", compared="tienda comercial", rule="land_use.store", evidence=[ev("u", "u.pdf", 1, "USO HABITACIONAL")])
    f2 = finding("V-01", "predial_year_below_min", "El predial es del ejercicio 2019, anterior a 2020.", "high", file_id="p", filename="p.pdf", page=1, found="2019", compared="mínimo 2020",
                 rule="predial.min_year", evidence=[ev("p", "p.pdf", 1, "EJERCICIO 2019")])
    reqs = [{"requirement_id": "USO-compat", "status": "incumplido", "description": "", "justification": ""}, {"requirement_id": "PREDIAL-year-p.pdf", "status": "incumplido", "description": "", "justification": ""}]
    p = presenter.build(result(FOUR, [f1, f2], reqs, verdict="no_cumple"))
    st = {c["type"]: c["status"] for c in p["documents"]}
    assert st["uso_suelo"] == "no_cumple" and st["predial"] == "no_cumple" and st["escrituras"] == "correcto"
    titles = [i["title"] for i in p["findings"]]
    assert "El predial es de un año anterior al permitido" in titles and "El uso de suelo no permite confirmar el proyecto" in titles
    assert p["verdict"] == "no_cumple" and "no cumple" in p["final_text"].lower()
    assert_plain(p)


def test_incumplimiento_no_verificado_no_se_marca_como_no_cumple():
    f = finding("V-01", "predial_year_below_min", "El predial es del ejercicio 2019, anterior a 2020.", "high", file_id="p", filename="p.pdf", found="2019", rule="predial.min_year",
                evidence=[ev("p", "p.pdf", 1, "TEXTO", verified=False)], audit="no_verificado")
    reqs = [{"requirement_id": "PREDIAL-year-p.pdf", "status": "incumplido", "description": "", "justification": ""}]
    p = presenter.build(result(FOUR, [f], reqs))
    assert {c["type"]: c["status"] for c in p["documents"]}["predial"] == "requiere_revision"
    assert "no se pudo comprobar en el original" in json.dumps(p["findings"][0]["evidence"], ensure_ascii=False) or p["findings"][0]["evidence"][0]["verified"] is False


def test_incertidumbre_se_explica_y_pendiente_no_es_error():
    f = finding("V-03", "predial_year_unknown", "No se identificó el año del predial; pendiente de verificación.", "medium", rule="predial.min_year")
    p = presenter.build(result(FOUR, [f]))
    it = p["findings"][0]
    assert "no permite identificar con claridad el año" in it["text"] and "copia más legible" in it["recommendation"]
    assert {c["type"]: c["status"] for c in p["documents"]}["predial"] in ("pendiente", "correcto")
    assert_plain(p)


# --------------------------------------------------------------- garantias de fidelidad
def test_no_modifica_el_resultado_ni_oculta_hallazgos_ni_cambia_el_veredicto():
    fs = [finding("C-01", "inconsistency", "x", found="Escrituras: 1", compared="Predial: 2", rule="cross_checks.owner", evidence=[ev("e", "e.pdf", 1, "a")]),
          finding("V-09", "tipo_nuevo_desconocido", "Algo nuevo que revisar.", "high")]
    r = result(FOUR, fs, verdict="requiere_revision", confidence=0.57)
    before = copy.deepcopy(r)
    p = presenter.build(r)
    assert r == before                                   # no altera datos, confianza ni estados
    assert p["verdict"] == "requiere_revision" and p["confidence"] == 0.57
    assert len(p["findings"]) == 2                       # incluso un tipo que la capa no conoce se muestra (nunca se omite)
    assert any("Algo nuevo" in i["text"] for i in p["findings"])


def test_los_tres_veredictos_se_conservan():
    for v in ("cumple", "requiere_revision", "no_cumple"):
        assert presenter.build(result(FOUR, verdict=v))["verdict"] == v


def test_baja_confianza_se_advierte():
    p = presenter.build(result(FOUR, verdict="requiere_revision", confidence=0.4))
    assert "confianza del análisis es baja" in p["final_text"]
