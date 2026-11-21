"""Flujo web: acceso con codigo, enlace creado por 3B, formulario de 4 documentos, activacion automatica, eliminacion y vistas."""
import re
import time

import pymupdf
import pyotp
import pytest
from fastapi.testclient import TestClient

from services.encryption_service import generate_master_key


def tiny_pdf(text="documento de prueba") -> bytes:
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), text)
    return d.tobytes()


SLOT_KEYS = ["escrituras", "predial", "alineamiento_numero_oficial", "uso_suelo"]


def send_all(pub, token, submit=True):
    """Sube un documento en cada uno de los 4 lugares (obligatorios) y envia."""
    for k in SLOT_KEYS:
        r = pub.post(f"/r/{token}/archivos", data={"slot": k}, files=[("files", (f"{k}.pdf", tiny_pdf(k), "application/pdf"))])
        assert r.json()["results"][0]["status"] == "formato_valido"
    return pub.post(f"/r/{token}/enviar") if submit else None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INMO_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("INMO_MASTER_KEY", generate_master_key())
    monkeypatch.setenv("INMO_SESSION_SECRET", "secreto-de-prueba-" + "x" * 32)
    import services.encryption_service as es
    import services.form_service as fs
    es._service, fs._store = None, None
    fs._handlers["expediente_recibido"].clear()
    calls = []
    # Se sustituye SOLO el analisis pesado; el evento y su suscripcion son los reales.
    monkeypatch.setattr(fs.ExpedienteStore, "run_analysis", lambda self, exp_id: calls.append(exp_id))
    from services import mfa_service as auth
    auth._fails.clear()          # el bloqueo por intentos vive en memoria: cada prueba empieza limpia
    secret, _ = auth.create_user("abogado", "una-clave-larga-123")
    import app as appmod
    return TestClient(appmod.app, follow_redirects=False), secret, calls, fs


def login(client, secret):
    assert client.post("/panel/login", data={"username": "abogado", "password": "una-clave-larga-123"}).status_code == 200
    assert client.post("/panel/mfa", data={"code": pyotp.TOTP(secret).now()}).status_code == 303
    return client


def new_link(client, fs, name="Juan Pérez", kind="store"):
    r = client.post("/panel/nuevo", data={"applicant": name, "project_type": kind})
    assert r.status_code == 303
    exp_id = r.headers["location"].rsplit("/", 1)[1]
    return exp_id, fs.get_store().make_token(exp_id)


def test_panel_exige_autenticacion(env):
    client, *_ = env
    assert client.get("/panel").status_code == 303
    assert client.get("/panel/expedientes").status_code == 303
    assert client.get("/panel/expedientes/EXP-X").status_code == 303
    assert client.get("/panel/qr/EXP-X.png").status_code == 303


def test_contrasena_sola_no_da_acceso_y_codigo_incorrecto_se_rechaza(env):
    client, secret, *_ = env
    client.post("/panel/login", data={"username": "abogado", "password": "una-clave-larga-123"})
    assert client.get("/panel").status_code == 303          # falta el codigo
    assert client.post("/panel/mfa", data={"code": "000000"}).status_code == 401
    assert client.get("/panel").status_code == 303
    assert client.post("/panel/login", data={"username": "abogado", "password": "mala"}).status_code == 401


def test_el_enlace_exige_nombre_y_tipo(env):
    client, secret, calls, fs = env
    login(client, secret)
    for data in ({"applicant": "", "project_type": "store"}, {"applicant": "ACME", "project_type": ""}):
        r = client.post("/panel/nuevo", data=data)
        assert r.status_code == 303 and "/panel/nuevo?error=" in r.headers["location"]
    assert fs.get_store().list_ids() == []


def test_formulario_publico_minimo_cuatro_documentos_sin_datos_ni_jerga(env):
    client, secret, calls, fs = env
    login(client, secret)
    exp_id, token = new_link(client, fs)
    html = TestClient(client.app).get(f"/r/{token}").text
    assert html.count('class="slot"') == 4
    for t in ("Escrituras", "Predial", "Alineamiento y Número Oficial", "Uso de Suelo"):
        assert t in html
    assert 'name="applicant"' not in html and 'name="project_type"' not in html     # el nombre y el tipo los pone 3B
    assert html.count("(obligatorio)") == 4 and ".zip" not in html.lower() and "Juan" not in html
    for word in ("agente", "OCR", "regla", "cifr", "MFA", "magic"):
        assert word.lower() not in html.lower(), word


def test_flujo_completo_carga_por_documento_envio_y_activacion_automatica(env):
    client, secret, calls, fs = env
    login(client, secret)
    exp_id, token = new_link(client, fs, "Inmobiliaria Norte", "cedis")
    pub = TestClient(client.app, follow_redirects=False)
    assert pub.get("/r/token-falso").status_code == 404
    same = tiny_pdf()
    up = lambda slot, name, data: pub.post(f"/r/{token}/archivos", data={"slot": slot}, files=[("files", (name, data, "application/pdf"))]).json()["results"][0]
    assert up("escrituras", "escritura.pdf", same)["status"] == "formato_valido"
    bad = up("predial", "falso.pdf", b"MZ" + b"\0" * 900)
    assert bad["status"] == "formato_invalido" and bad["rejection"]["hint"]
    assert up("uso_suelo", "copia.pdf", same)["status"] == "duplicado"
    zipped = up("predial", "todo.zip", b"PK\x03\x04" + b"0" * 50)
    assert zipped["status"] == "formato_invalido" and "PDF" in zipped["rejection"]["hint"]     # el formulario solo recibe PDF o fotos
    meta = fs.get_store().get_meta(exp_id)
    assert {f["name"]: f["declared"] for f in meta["files"]}["escritura.pdf"] == "escrituras"
    assert calls == []
    r = pub.post(f"/r/{token}/enviar")                            # solo hay 1 de los 4 documentos obligatorios
    assert r.status_code == 400 and "Faltan estos documentos" in r.json()["error"] and "Predial" in r.json()["error"] and calls == []
    for k in ("predial", "alineamiento_numero_oficial", "uso_suelo"):
        pub.post(f"/r/{token}/archivos", data={"slot": k}, files=[("files", (f"{k}.pdf", tiny_pdf(k), "application/pdf"))])
    assert pub.post(f"/r/{token}/enviar").status_code == 200
    time.sleep(0.3)
    assert calls == [exp_id]                                      # el analisis arranco solo, sin accion del trabajador
    assert pub.post(f"/r/{token}/enviar").status_code == 400      # mientras se revisa no se puede enviar otra vez
    assert "Estamos revisando tus documentos" in pub.get(f"/r/{token}").text
    meta = fs.get_store().get_meta(exp_id)
    assert meta["applicant"] == "Inmobiliaria Norte" and meta["project_type"] == "cedis"


def test_envio_sin_documentos_validos_se_rechaza(env):
    client, secret, calls, fs = env
    login(client, secret)
    exp_id, token = new_link(client, fs)
    r = TestClient(client.app).post(f"/r/{token}/enviar")
    assert r.status_code == 400 and "Faltan estos documentos" in r.json()["error"] and calls == []


def test_nuevas_solicitudes_y_pantallas_del_panel_sin_jerga_ni_reglas(env):
    client, secret, calls, fs = env
    login(client, secret)
    exp_id, token = new_link(client, fs, "Familia López")
    send_all(TestClient(client.app), token)
    home = client.get("/panel").text
    assert "Nuevas por abrir" in home and "Familia López" in home and 'class="nuevo"' in home
    assert 'class="nuevo"' in client.get("/panel/expedientes").text                       # aviso junto al boton
    ready = client.get(f"/panel/enlaces/{exp_id}")
    assert ready.status_code == 200 and "/panel/qr/" in ready.text and "wa.me" in ready.text
    assert client.get(f"/panel/qr/{exp_id}.png").content[:4] == b"\x89PNG"
    detail = client.get(f"/panel/expedientes/{exp_id}")                                      # al abrirlo deja de ser "nuevo"
    assert detail.status_code == 200
    assert 'class="nuevo"' not in client.get("/panel/expedientes").text
    for page in (home, detail.text, client.get("/panel/expedientes").text):
        low = page.lower()
        for word in ("agente", "regla", "ocr", "aes", "totp", "llm", "ollama"):
            assert word not in low, word
    assert "/panel/reglas" not in home


def test_estado_del_analisis_usa_pasos_sencillos(env):
    client, secret, calls, fs = env
    login(client, secret)
    exp_id, token = new_link(client, fs)
    send_all(TestClient(client.app), token)
    fs._progress[exp_id] = [{"stage": "Extrayendo datos con agentes especializados", "detail": "Agentes 3, 4, 5 y 6", "t": 1}]
    j = client.get(f"/panel/expedientes/{exp_id}/estado.json").json()
    assert j["step"] == 3 and set(j) == {"status", "step"}                                   # no expone nombres de agentes
    page = client.get(f"/panel/expedientes/{exp_id}").text
    assert "Leyendo los documentos" in page and "Preparando el resultado" in page and "Agente" not in page


def test_archivos_cifrados_y_eliminacion_solo_con_confirmacion(env):
    client, secret, calls, fs = env
    login(client, secret)
    exp_id, token = new_link(client, fs)
    pub = TestClient(client.app)
    pub.post(f"/r/{token}/archivos", data={"slot": "escrituras"}, files=[("files", ("a.pdf", tiny_pdf("PROPIETARIO JUAN PEREZ TEXTO SECRETO"), "application/pdf"))])
    store = fs.get_store()
    d = store.enc.data_dir(exp_id)
    blobs = [p for p in d.rglob("*") if p.is_file()]
    assert blobs and all(p.suffix == ".enc" for p in blobs) and all(b"JUAN PEREZ" not in p.read_bytes() for p in blobs)
    url = f"/panel/expedientes/{exp_id}/eliminar"
    assert "Confirma" in client.post(url, data={"confirm": "no"}).headers["location"].replace("+", " ").replace("%20", " ")
    assert store.enc.has_key(exp_id)                              # nada se borro sin confirmar
    assert TestClient(client.app, follow_redirects=False).post(url, data={"confirm": "ELIMINAR"}).status_code == 303   # sin sesion no se puede
    assert store.enc.has_key(exp_id)
    html = client.get(f"/panel/expedientes/{exp_id}?s=eliminar").text
    assert 'name="code"' not in html                              # el cuadro de confirmacion ya no pide codigo
    r = client.post(url, data={"confirm": "ELIMINAR"})
    assert r.status_code == 200 and "Expediente eliminado" in r.text
    assert not d.exists() and not store.enc.has_key(exp_id)
    assert client.get(f"/panel/expedientes/{exp_id}").status_code == 404
    from services.purge_service import receipts
    rc = receipts()[-1]
    assert rc["status"] == "completa" and rc["key_destroyed"] and "JUAN" not in str(rc)


def test_expediente_analizado_muestra_secciones_en_lenguaje_sencillo(env):
    client, secret, calls, fs = env
    login(client, secret)
    exp_id, token = new_link(client, fs, "Caso Demo")
    pub = TestClient(client.app)
    send_all(pub, token)
    store = fs.get_store()
    fid = store.get_meta(exp_id)["files"][0]["file_id"]
    ev = lambda page, frag: {"file_id": fid, "filename": "p.pdf", "page": page, "fragment": frag, "verified": True}
    result = {"expediente_id": exp_id, "project_type": "store", "documents": [{"file_id": fid, "filename": "p.pdf", "doc_type": "predial", "classification_confidence": .9, "classification_reason": "", "pages_total": 1,
              "ocr_engine": "x", "elapsed_ms": 1, "pages": [{"page": 1, "status": "ok", "source": "ocr", "ocr_confidence": .9, "issues": []}], "unreadable_pages": [], "has_text": True}],
              "extractions": [{"file_id": fid, "filename": "p.pdf", "doc_type": "predial", "status": "ok", "fields": {"owner": {"value": "ROSA QUINTANAR", "status": "present", "confidence": .9, "evidence": [ev(1, "PROPIETARIO ROSA QUINTANAR")]}}}],
              "requirements": [{"requirement_id": "PREDIAL-year-p", "description": "Predial del ejercicio 2020 o posterior", "status": "cumplido", "justification": "El predial corresponde al ejercicio 2025 (mínimo 2020).", "evidence": [ev(1, "2025")]}],
              "findings": [{"finding_id": "V-01", "description": "x", "finding_type": "validity_unknown", "severity": "low", "agent": "A7", "confidence": .5, "audit_status": "verificado", "evidence": []}],
              "comparisons": [], "audit": {"stats": {}, "issues": []}, "recommendation": {"verdict": "requiere_revision", "confidence": .7, "explanation": "x", "reason": "x", "disclaimer": "No sustituye al abogado."},
              "agent_log": [], "timings_ms": {"total": 40000}, "rules_reference": {"version": "1", "sha256": "a" * 64}}
    store.enc.put_json(exp_id, "resultado", "analisis", result)
    meta = store.get_meta(exp_id); meta.update(status="analizado", verdict="requiere_revision"); store.put_meta(exp_id, meta)
    pages = {s: client.get(f"/panel/expedientes/{exp_id}?s={s}") for s in ("resumen", "observaciones", "documentos", "requisitos", "datos", "eliminar")}
    assert all(r.status_code == 200 for r in pages.values())
    assert "Requiere revisión" in pages["resumen"].text and "Documentos correctos" in pages["resumen"].text
    assert "Predial del año 2020 o posterior" in pages["requisitos"].text and "ejercicio" not in pages["requisitos"].text
    assert "ROSA QUINTANAR" in pages["datos"].text and "Eliminar expediente" in pages["eliminar"].text
    for name, r in pages.items():
        low = r.text.lower()
        for word in ("agente", "regla", "ocr", "llm", "confianza insuficiente", "a7"):
            assert word not in low, (name, word)


def test_reglas_invalidas_no_se_guardan_y_no_estan_en_el_menu(env):
    client, secret, *_ = env
    login(client, secret)
    assert "reglas" not in client.get("/panel").text.lower()
    assert client.get("/panel/reglas").status_code == 200
    r = client.post("/panel/reglas", data={"text": "meta: {version: x}"})
    assert r.status_code == 400 and "errores de estructura" in r.text


def test_analisis_interrumpido_por_reinicio_se_retoma_y_solicitud_en_espera_se_puede_eliminar(env):
    client, secret, calls, fs = env
    login(client, secret)
    exp_id, token = new_link(client, fs)
    pub = TestClient(client.app)
    send_all(pub, token)
    time.sleep(0.3)
    store = fs.get_store()
    meta = store.get_meta(exp_id); meta["status"] = "en_analisis"; store.put_meta(exp_id, meta)   # asi queda si el servidor se apaga a la mitad
    calls.clear()
    assert store.resume_pending() == [exp_id]
    time.sleep(0.3)
    assert calls == [exp_id]
    page = client.get(f"/panel/expedientes/{exp_id}").text           # en espera/analizando tambien hay boton para eliminar
    assert "Eliminar solicitud" in page and 'name="confirm"' in page
    assert client.post(f"/panel/expedientes/{exp_id}/eliminar", data={"confirm": "ELIMINAR"}).status_code == 200
    assert not store.enc.has_key(exp_id)


def test_el_enlace_sigue_abierto_y_permite_reenviar_con_nuevo_analisis(env):
    client, secret, calls, fs = env
    login(client, secret)
    exp_id, token = new_link(client, fs, "Familia Ruiz")
    pub = TestClient(client.app)
    assert send_all(pub, token).status_code == 200
    time.sleep(0.3)
    assert calls == [exp_id]
    store = fs.get_store()
    meta = store.get_meta(exp_id)                                    # simula que el analisis termino y la 3B ya lo abrio
    meta["files"] = [{**f, "status": "analizado"} for f in meta["files"]]
    meta["files"][1]["status"] = "ilegible"                           # un documento no se pudo leer
    meta.update(status="analizado", verdict="requiere_revision", opened_at="2026-01-01T00:00:00+00:00")
    store.put_meta(exp_id, meta)
    page = pub.get(f"/r/{token}").text                                # el enlace sigue abierto: muestra lo enviado
    assert "Ya recibimos tus documentos" in page and "Enviar de nuevo" in page and "escrituras.pdf" in page
    assert "No se pudo leer bien" in page                             # y le avisa cual archivo debe cambiar
    first = meta["files"][1]                                          # cambia el documento ilegible por uno nuevo
    assert pub.post(f"/r/{token}/quitar/{first['file_id']}").json()["removed"] is True
    assert pub.post(f"/r/{token}/enviar").status_code == 400          # sin ese documento ya no estan los 4
    pub.post(f"/r/{token}/archivos", data={"slot": first["declared"]}, files=[("files", ("nuevo.pdf", tiny_pdf("nuevo"), "application/pdf"))])
    calls.clear()
    assert pub.post(f"/r/{token}/enviar").status_code == 200
    time.sleep(0.3)
    assert calls == [exp_id]                                          # se vuelve a analizar
    meta = store.get_meta(exp_id)
    assert meta["status"] == "recibido" and meta["resubmissions"] == 1 and meta["verdict"] is None and meta["opened_at"] is None   # vuelve a salir como "Nueva"
    assert all(f["status"] in ("formato_valido",) for f in meta["files"] if f.get("declared"))
    assert "Estamos revisando" in pub.get(f"/r/{token}").text
