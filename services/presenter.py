"""Capa de presentacion de resultados en lenguaje sencillo.

SOLO traduce el resultado ya calculado por los agentes (documentos, requisitos, hallazgos, comparaciones, auditoria y
recomendacion) a explicaciones claras. NO vuelve a evaluar documentos, NO cambia datos, niveles de confianza, reglas ni
el veredicto, y NO oculta hallazgos: cada hallazgo tecnico produce un elemento visible con documento, pagina y evidencia.
Si un resultado trae informacion que esta capa no conoce, se muestra con una redaccion generica en vez de omitirse.
"""
from __future__ import annotations

import re
from typing import Optional

DOC_ORDER = ["escrituras", "predial", "alineamiento_numero_oficial", "uso_suelo"]
DOC_LABEL = {"escrituras": "Escrituras", "predial": "Predial", "alineamiento_numero_oficial": "Alineamiento y Número Oficial",
             "uso_suelo": "Uso de Suelo", "desconocido": "Documento no identificado"}
# Qué permite comprobar cada documento (según la guía del reto)
DOC_PROVES = {
    "escrituras": "quién es el propietario, la dirección y la superficie del terreno",
    "predial": "el propietario o contribuyente, la clave del predio, la superficie registrada y el año del pago",
    "alineamiento_numero_oficial": "la dirección oficial y el número oficial del inmueble",
    "uso_suelo": "para qué se puede usar el inmueble y qué restricciones tiene",
}
STATUS_LABEL = {"correcto": "Correcto", "requiere_revision": "Requiere revisión", "no_cumple": "No cumple", "faltante": "Faltante",
                "ilegible": "Ilegible", "pendiente": "Pendiente"}
STATUS_HELP = {
    "correcto": "No se encontraron problemas en las revisiones realizadas.",
    "requiere_revision": "Se encontró información que necesita comprobarse.",
    "no_cumple": "Se identificó un requisito que no se satisface.",
    "faltante": "El documento no fue recibido.",
    "ilegible": "No fue posible leer correctamente el documento.",
    "pendiente": "Todavía no hay información suficiente para evaluarlo.",
}
VERDICT_LABEL = {"cumple": "Cumple", "requiere_revision": "Requiere revisión", "no_cumple": "No cumple"}
SEVERITY_LABEL = {"high": "Importante", "medium": "A revisar", "low": "Menor"}
FIELD_LABEL = {"owner": "nombre del propietario", "property_address": "dirección", "address": "dirección", "official_address": "dirección oficial",
               "property_id": "clave o identificación del predio", "land_surface_m2": "superficie del terreno", "registered_surface_m2": "superficie registrada",
               "surface_m2": "superficie", "applicable_surface": "superficie aplicable", "deed_number": "número de escritura", "notary": "notario",
               "other_details": "otros datos del inmueble", "predial_account": "cuenta o clave del predial", "fiscal_year": "año del predial",
               "amount_status": "importe o estado del pago", "official_number": "número oficial", "alignment": "alineamiento", "issue_date": "fecha de emisión",
               "validity": "vigencia", "issuing_authority": "autoridad que emite el documento", "authorized_use": "uso de suelo autorizado",
               "permitted_activities": "actividades permitidas", "restrictions": "restricciones"}
PROGRESS_STEPS = ["Recibimos los documentos", "Leyendo los documentos", "Identificando cada documento", "Revisando la información",
                  "Comparando los documentos entre sí", "Verificando los datos", "Preparando el resultado"]


def progress_step(stage: str) -> int:
    """Traduce la etapa interna del analisis a un paso sencillo (0-6). No expone nombres de agentes."""
    t = (stage or "").lower()
    for key, idx in (("recib", 0), ("leyendo", 1), ("clasific", 2), ("extray", 3), ("compar", 4), ("valid", 4), ("audit", 5), ("recomend", 6)):
        if key in t:
            return idx
    return 0


PROJECT_LABEL = {"store": "tienda comercial", "cedis": "centro de distribución (CEDIS)"}
CRITICAL_TYPES = {"predial_year_below_min", "document_expired", "land_use_incompatible"}


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def _split_doc_value(text: Optional[str]) -> tuple[str, str]:
    """'Escrituras: 500 m2' -> ('Escrituras', '500 m2')."""
    if text and ": " in text:
        a, b = text.split(": ", 1)
        return a, b
    return "", text or ""


def _doc_type_of(result: dict, file_id: Optional[str] = None, filename: Optional[str] = None) -> Optional[str]:
    for d in result.get("documents", []):
        if (file_id and d["file_id"] == file_id) or (filename and d["filename"] == filename):
            return d["doc_type"]
    return None


def _docs_refs(result: dict, f: dict) -> list[dict]:
    refs, seen = [], set()
    for e in f.get("evidence") or []:
        dt = _doc_type_of(result, e.get("file_id"), e.get("filename"))
        key = (dt, e.get("page"))
        if key not in seen:
            seen.add(key)
            refs.append({"label": DOC_LABEL.get(dt, e.get("filename") or "Documento"), "page": e.get("page")})
    if not refs and f.get("filename"):
        dt = _doc_type_of(result, f.get("file_id"), f.get("filename"))
        refs.append({"label": DOC_LABEL.get(dt, f["filename"]), "page": f.get("page")})
    return refs


def _evidence(result: dict, f: dict) -> list[dict]:
    out = []
    for e in f.get("evidence") or []:
        dt = _doc_type_of(result, e.get("file_id"), e.get("filename"))
        out.append({"doc": DOC_LABEL.get(dt, e.get("filename") or "Documento"), "page": e.get("page"), "fragment": e.get("fragment"), "verified": e.get("verified")})
    return out


def _finding_item(result: dict, f: dict, project: Optional[str]) -> Optional[dict]:
    t, desc = f.get("finding_type"), f.get("description", "")
    found, compared = f.get("found_value"), f.get("compared_value")
    refs = _docs_refs(result, f)
    dtypes = {_doc_type_of(result, e.get("file_id"), e.get("filename")) for e in (f.get("evidence") or [])}
    if f.get("file_id"):
        dtypes.add(_doc_type_of(result, f.get("file_id"), f.get("filename")))
    base = {"id": f.get("finding_id"), "severity": f.get("severity", "medium"), "docs": refs, "evidence": _evidence(result, f),
            "confidence": f.get("confidence"), "audit": f.get("audit_status"), "found": found, "compared": compared, "type": t, "rule": f.get("rule"),
            "document_types": sorted(x for x in dtypes if x in DOC_ORDER)}
    if t == "missing_required_document":
        return None  # se muestra en la sección "Documentos faltantes"
    if t == "predial_year_below_min":
        m = re.search(r"anterior a (\d{4})", desc)
        return {**base, "title": "El predial es de un año anterior al permitido",
                "text": f"El documento predial corresponde al año {found}. El requisito pide un predial del año {m.group(1) if m else 'indicado'} o posterior.",
                "why": "Con un predial de un año anterior no se cumple el requisito de actualidad.",
                "recommendation": "Solicitar un documento predial que corresponda al año requerido."}
    if t in ("predial_year_unknown", "land_use_undetermined") and not base["document_types"]:
        dt_ = "predial" if t == "predial_year_unknown" else "uso_suelo"
        base["document_types"] = [dt_]
        if not base["docs"]:
            base["docs"] = [{"label": DOC_LABEL[dt_], "page": None}]
    if t == "predial_year_unknown":
        return {**base, "title": "No se pudo identificar el año del predial",
                "text": "El documento no permite identificar con claridad el año al que corresponde.",
                "why": "Sin el año no se puede comprobar si el predial cumple con el requisito.",
                "recommendation": "Pedir una copia más legible del predial, donde se vea el año."}
    if t == "document_expired":
        who = refs[0]["label"] if refs else "El documento"
        return {**base, "title": "La vigencia del documento ya venció",
                "text": f"{who} indica una vigencia hasta {found}. Esa fecha ya pasó.", "why": "Un documento vencido puede no ser aceptado para continuar con el proceso.",
                "recommendation": "Solicitar un documento vigente o confirmar la vigencia con la autoridad que lo emitió."}
    if t == "validity_unknown":
        who = re.match(r"([^:]+):", desc)
        rev = {v: k for k, v in DOC_LABEL.items()}
        dt_ = rev.get(who.group(1)) if who else None
        if dt_ and not base["document_types"]:
            base["document_types"] = [dt_]
            base["docs"] = base["docs"] or [{"label": who.group(1), "page": None}]
        return {**base, "title": "No se encontró la vigencia del documento",
                "text": f"{who.group(1) if who else 'El documento'} no indica hasta cuándo es válido, o no se pudo leer ese dato.",
                "why": "Sin la vigencia no se puede comprobar que el documento siga siendo válido.",
                "recommendation": "Revisar el documento original o confirmar su vigencia con quien lo emitió."}
    if t == "land_use_incompatible":
        reason = desc.split(": ", 1)[1] if ": " in desc else ""
        dest = PROJECT_LABEL.get(project or "", compared or "el proyecto indicado")
        return {**base, "title": "El uso de suelo no permite confirmar el proyecto",
                "text": f"El documento de Uso de Suelo autoriza «{found}» y el proyecto indicado es {dest}. " + (reason if reason else ""),
                "why": "Si el uso autorizado no coincide con el proyecto, el inmueble podría no poder usarse como se planea.",
                "recommendation": "Revisar las actividades autorizadas y sus restricciones en el documento de Uso de Suelo antes de continuar."}
    if t == "land_use_undetermined":
        return {**base, "title": "No se pudo confirmar si el uso de suelo permite el proyecto",
                "text": "El documento de Uso de Suelo no permite saber con claridad si el proyecto indicado está permitido, o falta indicar si es tienda o CEDIS.",
                "why": "Sin esa confirmación no se puede saber si el inmueble sirve para el proyecto.",
                "recommendation": "Revisar el documento de Uso de Suelo, sus actividades permitidas y sus restricciones."}
    if t == "inconsistency":
        da, va = _split_doc_value(found)
        db, vb = _split_doc_value(compared)
        have = {d["label"].lower() for d in base["docs"]}
        for lbl in (da, db):  # los dos documentos comparados siempre se citan, aunque falte la cita textual de uno
            canon = {"uso de suelo": "Uso de Suelo", "alineamiento": "Alineamiento y Número Oficial"}.get(lbl.lower(), lbl)
            if canon and canon.lower() not in have:
                base["docs"].append({"label": canon, "page": None})
                have.add(canon.lower())
        rule = f.get("rule") or ""
        pair = f"{da or 'Un documento'} indica «{va}», mientras que {db or 'otro documento'} indica «{vb}»."
        if rule.endswith("surface"):
            diff = re.search(r"Diferencia de ([\d.,]+) m²", desc)
            extra = f" La diferencia es de {diff.group(1)} m²." if diff else ""
            if "No hay tolerancia" in desc:
                extra += " Todavía no hay una diferencia permitida definida, por eso se pide revisarla."
            return {**base, "title": "Diferencia en la superficie del inmueble", "text": f"Los documentos muestran superficies distintas para el mismo inmueble. {pair}{extra}",
                    "why": "Con superficies diferentes no es posible saber cuál es la correcta.",
                    "recommendation": "Pedir la aclaración de la diferencia y confirmar cuál es la superficie correcta antes de continuar."}
        if rule.endswith("owner"):
            return {**base, "title": "El nombre del propietario no coincide", "text": f"El nombre del propietario no es igual en los documentos. {pair}",
                    "why": "Hay que confirmar que ambos documentos se refieren a la misma persona.",
                    "recommendation": "Revisar la diferencia y, si hace falta, solicitar un documento que confirme quién es el propietario."}
        if rule.endswith("address"):
            return {**base, "title": "La dirección no coincide entre documentos", "text": f"La dirección del inmueble no es igual en los documentos. {pair}",
                    "why": "Podría ser un error de captura o tratarse de inmuebles distintos.",
                    "recommendation": "Confirmar cuál es la dirección correcta y pedir el documento corregido si hace falta."}
        if rule.endswith("parcel_id"):
            return {**base, "title": "La clave del predio no coincide", "text": f"La clave o identificación del predio no es igual en los documentos. {pair}",
                    "why": "La clave identifica al inmueble; si es distinta, los documentos podrían ser de inmuebles diferentes.",
                    "recommendation": "Confirmar que todos los documentos correspondan al mismo inmueble."}
        return {**base, "title": "Hay información que no coincide entre documentos", "text": pair,
                "why": "Una diferencia puede ser un error o indicar que los documentos no corresponden al mismo inmueble.",
                "recommendation": "Revisar la diferencia y pedir una aclaración."}
    # tipo desconocido para esta capa: nunca se omite
    return {**base, "title": "Punto que necesita revisión", "text": desc, "why": "El análisis encontró algo que conviene comprobar.",
            "recommendation": "Revisar este punto directamente en el documento."}


def _req_doc_type(req_id: str) -> Optional[str]:
    if req_id.startswith("PREDIAL"):
        return "predial"
    if req_id.startswith("USO"):
        return "uso_suelo"
    m = re.match(r"(?:VIG|DOC)-(.+)", req_id)
    return m.group(1) if m else None


_FIELD_NOUN = {"surface": "La superficie", "owner": "El nombre del propietario", "address": "La dirección", "parcel_id": "La clave del predio"}


def _client_line(i: dict) -> str:
    """Una linea corta y clara para la persona que envio los documentos."""
    t = i.get("type")
    if t == "unreadable":
        return "No se pudo leer. Súbelo de nuevo con una foto o un escaneo más claro."
    if t == "predial_year_below_min":
        m = re.search(r"(\d{4})", i.get("compared") or "")
        return f"Es del año {i.get('found')}. Envía uno del {m.group(1)} o más reciente." if m else f"Es del año {i.get('found')}. Envía uno más reciente."
    if t == "predial_year_unknown":
        return "No se alcanza a ver el año. Envía una copia más clara."
    if t == "document_expired":
        return f"La vigencia ya venció ({i.get('found')}). Envía uno vigente."
    if t == "land_use_incompatible":
        return "El uso de suelo no permite el proyecto indicado. Revisa con quien lo emitió."
    if t == "land_use_undetermined":
        return "No queda claro si el uso de suelo permite el proyecto. Envía el documento completo y legible."
    if t == "inconsistency":
        da, va = _split_doc_value(i.get("found"))
        db, vb = _split_doc_value(i.get("compared"))
        noun = _FIELD_NOUN.get((i.get("rule") or "").split(".")[-1], "Un dato")
        return f"{noun} no coincide: {da or 'un documento'} dice «{va}» y {db or 'otro documento'} dice «{vb}». Confirma cuál es el correcto."
    return i.get("title", "") + "."


def share_message(items: list[dict], missing: list[str], verdict: str) -> str:
    """Texto breve, en lista y separado por documento, para compartir por WhatsApp o correo. Solo usa lo ya calculado."""
    by_doc: dict[str, list[str]] = {}
    cross, unread = [], []
    for i in items:
        if i["severity"] == "low" or i.get("type") in ("unverified", "low_quality"):
            continue                      # observaciones menores: no se piden al cliente
        line = _client_line(i)
        if i.get("type") == "inconsistency":
            cross.append(line)
        elif i.get("type") == "unreadable":
            unread.append(f"{i.get('filename') or 'Un archivo'}: {line}")
        else:
            label = (i["docs"][0]["label"] if i.get("docs") else "Otros")
            by_doc.setdefault(label, []).append(line)
    if not (missing or by_doc or cross or unread):
        return "Hola, revisamos tus documentos y todo está en orden. ¡Gracias!" if verdict == "cumple" else "Hola, revisamos tus documentos y estamos terminando la revisión. Te avisamos si necesitamos algo más. ¡Gracias!"
    out = ["Hola, revisamos tus documentos. Para continuar necesitamos lo siguiente:"]
    if missing:
        out += ["", "*Documentos que faltan*"] + [f"- {m}" for m in missing]
    for label in [DOC_LABEL[t] for t in DOC_ORDER] + [k for k in by_doc if k not in DOC_LABEL.values()]:
        if label in by_doc:
            out += ["", f"*{label}*"] + [f"- {l}" for l in by_doc[label]]
    if unread:
        out += ["", "*Archivos que no se pudieron leer*"] + [f"- {l}" for l in unread]
    if cross:
        out += ["", "*Datos que no coinciden entre documentos*"] + [f"- {l}" for l in cross]
    out += ["", "Gracias."]
    return "\n".join(out)


def _clean(text: str) -> str:
    """Quita referencias internas (reglas, MVP, comité) y palabras técnicas de textos escritos por otros agentes."""
    t = re.sub(r"\s*\((?:criterio provisional|regla provisional)[^)]*\)", "", text or "")
    for a, b in (("ejercicio", "año"), ("Ejercicio", "Año"), ("mínimo configurado", "mínimo requerido"), ("regla configurada", "requisito"), ("configurada", "requerida")):
        t = t.replace(a, b)
    return t.strip()


REQ_STATUS = {"cumplido": "Cumple", "incumplido": "No cumple", "pendiente": "Por confirmar"}


def build(result: dict, meta: Optional[dict] = None) -> dict:
    """Devuelve el resumen en lenguaje sencillo. No modifica `result`."""
    project = result.get("project_type")
    rec = result["recommendation"]
    docs = result.get("documents", [])
    present = {d["doc_type"] for d in docs if d["doc_type"] in DOC_ORDER}
    unreadable = [d for d in docs if d.get("unreadable_pages") or not d.get("has_text")]
    lowq = [(d, [p["page"] for p in d.get("pages", []) if p["status"] == "baja_calidad"]) for d in docs]
    lowq = [(d, pg) for d, pg in lowq if pg]
    missing = [t for t in DOC_ORDER if t not in present]

    # ---- hallazgos en lenguaje sencillo (uno por hallazgo tecnico; ninguno se omite)
    items = [i for i in (_finding_item(result, f, project) for f in result.get("findings", [])) if i]
    for d in unreadable:
        pages = d.get("unreadable_pages") or []
        where = f" (páginas {', '.join(map(str, pages))})" if pages else ""
        items.append({"id": f"L-{d['file_id'][:4]}", "severity": "medium", "type": "unreadable", "filename": d["filename"], "title": "Una imagen está borrosa o no se puede leer",
                      "text": f"El archivo «{d['filename']}»{where} no se puede leer correctamente. No se tomó ningún dato de esa parte para no equivocarnos.",
                      "why": "Sin poder leerlo no se puede comprobar su contenido.", "recommendation": "Cargar una fotografía o un escaneo más claro del documento.",
                      "docs": [{"label": DOC_LABEL.get(d["doc_type"], d["filename"]), "page": pages[0] if pages else None}], "evidence": [], "confidence": None, "audit": None,
                      "found": None, "compared": None, "document_types": [d["doc_type"]] if d["doc_type"] in DOC_ORDER else []})
    for d, pg in lowq:
        items.append({"id": f"B-{d['file_id'][:4]}", "severity": "low", "type": "low_quality", "title": "La calidad de algunas páginas es baja",
                      "text": f"En «{d['filename']}», las páginas {', '.join(map(str, pg))} se leyeron con dificultad. Los datos se tomaron, pero pueden tener errores.",
                      "why": "Una imagen de baja calidad aumenta la posibilidad de leer mal un dato.", "recommendation": "Si algún dato importante parece dudoso, pedir una imagen más clara.",
                      "docs": [{"label": DOC_LABEL.get(d["doc_type"], d["filename"]), "page": pg[0]}], "evidence": [], "confidence": None, "audit": None, "found": None, "compared": None,
                      "document_types": [d["doc_type"]] if d["doc_type"] in DOC_ORDER else []})
    grouped: dict[str, list[str]] = {}
    for iss in result.get("audit", {}).get("issues", []):
        dt = _doc_type_of(result, filename=iss.get("file")) or "desconocido"
        label = FIELD_LABEL.get(iss.get("field"), "otro dato")
        grouped.setdefault(dt, [])
        if label not in grouped[dt]:
            grouped[dt].append(label)
    for k, (dt, labels) in enumerate(grouped.items()):
        who = DOC_LABEL.get(dt, "un documento")
        items.append({"id": f"A-{k+1:02d}", "severity": "low", "type": "unverified", "title": f"Algunos datos de {who} no se pudieron comprobar" if dt != "desconocido" else "Algunos datos no se pudieron comprobar",
                      "text": f"No se encontró en el documento original el texto que respalda estos datos: {', '.join(labels)}. Por eso no se consideran confirmados.",
                      "why": "Un dato sin respaldo puede ser una lectura equivocada.", "recommendation": "Revisar estos datos directamente en el documento original.",
                      "docs": [{"label": who, "page": None}], "evidence": [], "confidence": None, "audit": "no_verificado", "found": None, "compared": None,
                      "document_types": [dt] if dt in DOC_ORDER else []})
    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda i: order.get(i["severity"], 1))
    for i in items:
        i["severity_label"] = SEVERITY_LABEL.get(i["severity"], "Importancia media")

    # ---- estado por documento
    confirmed_types = {f["finding_type"]: f for f in result.get("findings", []) if f.get("audit_status") == "verificado"}
    cards = []
    for t in DOC_ORDER:
        mine = [d for d in docs if d["doc_type"] == t]
        if not mine:
            note = DOC_PROVES[t]
            extra = " Se recibió un archivo que no se pudo leer; podría ser este documento." if unreadable else ""
            cards.append({"type": t, "label": DOC_LABEL[t], "status": "faltante", "reasons": [f"Sin este documento no se puede comprobar {note}.{extra}"], "files": []})
            continue
        reasons: list[str] = []
        bad = review = pend = False
        for q in result.get("requirements", []):
            if _req_doc_type(q["requirement_id"]) != t or q["requirement_id"].startswith("DOC-"):
                continue
            if q["status"] == "incumplido":
                ftype = {"PREDIAL": "predial_year_below_min", "VIG": "document_expired", "USO": "land_use_incompatible"}.get(q["requirement_id"].split("-")[0])
                if ftype in confirmed_types:
                    bad = True
                else:
                    review = True
            elif q["status"] == "pendiente":
                pend = True
        for i in items:
            if t in i.get("document_types", []):
                if i["type"] in ("inconsistency", "low_quality", "unverified"):
                    review = True
                elif i["type"] in ("predial_year_unknown", "validity_unknown", "land_use_undetermined"):
                    pend = True
                if i["type"] in CRITICAL_TYPES and i["type"] in confirmed_types:
                    bad = True
                reasons.append(i["title"])
        unread_here = any(d in unreadable for d in mine)
        if unread_here:
            status = "ilegible"
        elif bad:
            status = "no_cumple"
        elif review:
            status = "requiere_revision"
        elif pend or any(x["status"] == "error" for x in result.get("extractions", []) if x["doc_type"] == t):
            status = "pendiente"
        else:
            status = "correcto"
        cards.append({"type": t, "label": DOC_LABEL[t], "status": status, "reasons": list(dict.fromkeys(reasons)), "files": [d["filename"] for d in mine]})
    for d in unreadable:
        if d["doc_type"] not in DOC_ORDER:
            cards.append({"type": "desconocido", "label": f"Archivo «{d['filename']}»", "status": "ilegible",
                          "reasons": ["No se pudo leer, por eso no se identificó qué documento es."], "files": [d["filename"]]})
    for c in cards:
        c["status_label"], c["status_help"] = STATUS_LABEL[c["status"]], STATUS_HELP[c["status"]]

    # ---- faltantes
    miss = [{"label": DOC_LABEL[t], "text": f"Falta recibir el documento de {DOC_LABEL[t]}. Sin este documento no es posible comprobar {DOC_PROVES[t]}." +
             (" Se recibió un archivo que no se pudo leer; podría ser este documento." if unreadable else ""), "recommendation": f"Solicitar el documento de {DOC_LABEL[t]}."} for t in missing]

    # ---- resumen general y recomendacion final
    n_present = len(present)
    parts = []
    parts.append("Se recibieron los cuatro documentos del inmueble." if not missing else
                 f"Se recibieron {n_present} de los 4 documentos esperados. Falta: {', '.join(DOC_LABEL[t] for t in missing)}.")
    if unreadable:
        parts.append("1 archivo no se pudo leer." if len(unreadable) == 1 else f"{len(unreadable)} archivos no se pudieron leer.")
    attn = [i for i in items if i["severity"] in ("high", "medium")]
    if attn:
        uniq = list(dict.fromkeys(_lower_first(i["title"]) for i in attn))
        titles = uniq[:4]
        parts.append("Lo que necesita atención: " + "; ".join(titles) + ("." if len(uniq) <= 4 else f"; y {len(uniq) - 4} punto(s) más."))
    elif items:
        parts.append("Solo hay observaciones menores para revisar.")
    else:
        parts.append("No se encontraron problemas en las revisiones realizadas.")
    verdict = rec["verdict"]
    closing = {"cumple": "El expediente puede continuar con el proceso.",
               "requiere_revision": "Se recomienda revisar estos puntos antes de continuar con el proceso.",
               "no_cumple": "Es necesario corregir estos puntos antes de continuar con el proceso."}[verdict]
    summary = " ".join(parts + [closing])
    uncertain = ""
    if rec.get("confidence") is not None and rec["confidence"] < 0.6:
        uncertain = " La confianza del análisis es baja, así que conviene revisar los documentos con más cuidado."
    final = {"cumple": "Los documentos recibidos cumplen con los requisitos revisados y no se encontraron diferencias entre ellos.",
             "requiere_revision": "El expediente necesita una revisión antes de continuar. " + ("Faltan documentos o hay información que no se pudo comprobar. " if (missing or unreadable) else "Se encontró información que no coincide o que no se pudo comprobar. "),
             "no_cumple": "El expediente no cumple al menos uno de los requisitos revisados. "}[verdict]
    if verdict != "cumple" and attn:
        final += "Los puntos principales son: " + "; ".join(list(dict.fromkeys(_lower_first(i["title"]) for i in attn))[:3]) + "."
    final += uncertain
    reqs = [{"description": _clean(q["description"]), "status": q["status"], "status_label": REQ_STATUS[q["status"]], "text": _clean(q["justification"]),
             "evidence": [{"page": e.get("page"), "fragment": e.get("fragment")} for e in q.get("evidence", [])]} for q in result.get("requirements", [])]
    data = []
    for x in result.get("extractions", []):
        rows = [{"label": FIELD_LABEL.get(k, k), "value": f["value"], "page": (f["evidence"][0].get("page") if f.get("evidence") else None),
                 "fragment": (f["evidence"][0].get("fragment") if f.get("evidence") else None)} for k, f in x["fields"].items() if f.get("status") == "present" and f.get("value")]
        data.append({"label": DOC_LABEL.get(x["doc_type"], x["filename"]), "rows": rows, "note": "" if rows else "No se pudo leer información de este documento."})
    return {"verdict": verdict, "verdict_label": VERDICT_LABEL[verdict], "confidence": rec.get("confidence"), "summary": summary, "final_text": final.strip(),
            "documents": cards, "findings": items, "main": [i for i in items if i["severity"] in ("high", "medium")], "minor": [i for i in items if i["severity"] == "low"],
            "missing": miss, "problem_docs": [c for c in cards if c["status"] != "correcto"], "ok_docs": [c for c in cards if c["status"] == "correcto"],
            "share": share_message(items, [DOC_LABEL[t] for t in missing], verdict), "requirements": reqs, "data": data, "counts": {"ok": sum(c["status"] == "correcto" for c in cards), "problems": sum(c["status"] not in ("correcto", "faltante") for c in cards),
                                                           "missing": len(miss)}, "disclaimer": rec.get("disclaimer", "")}
