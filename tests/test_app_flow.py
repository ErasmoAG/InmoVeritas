"""Flujo web: MFA, enlace, carga validada, activacion automatica del analisis, purga y acceso protegido."""
import io
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
    secret, _ = auth.create_user("abogado", "una-clave-larga-123")
    import app as appmod
    return TestClient(appmod.app, follow_redirects=False), secret, calls, fs


def login(client, secret):
    assert client.post("/panel/login", data={"username": "abogado", "password": "una-clave-larga-123"}).status_code == 200
    r = client.post("/panel/mfa", data={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 303
    return client


def test_panel_exige_autenticacion(env):
    client, *_ = env
    assert client.get("/panel").status_code == 303
    assert client.get("/panel/expedientes/EXP-X").status_code == 303


def test_contrasena_sola_no_da_acceso_y_mfa_incorrecto_se_rechaza(env):
    client, secret, *_ = env
    client.post("/panel/login", data={"username": "abogado", "password": "una-clave-larga-123"})
    assert client.get("/panel").status_code == 303          # falta el segundo factor
    assert client.post("/panel/mfa", data={"code": "000000"}).status_code == 401
    assert client.get("/panel").status_code == 303
    assert client.post("/panel/login", data={"username": "abogado", "password": "mala"}).status_code == 401


def test_flujo_completo_enlace_carga_envio_y_activacion_automatica(env):
    client, secret, calls, fs = env
    login(client, secret)
    r = client.post("/panel/enlaces", data={"reference": "Terreno de prueba"})
    token = r.headers["location"].split("nuevo=")[1]
    public = TestClient(client.app, follow_redirects=False)    # contraparte: sin sesion
    assert public.get(f"/r/{token}").status_code == 200
    assert public.get("/r/token-falso").status_code == 404
    same = tiny_pdf()
    files = [("files", ("escritura.pdf", same, "application/pdf")),
             ("files", ("falso.pdf", b"MZ" + b"\0" * 900, "application/pdf")),
             ("files", ("copia.pdf", same, "application/pdf"))]
    res = public.post(f"/r/{token}/archivos", files=files).json()["results"]
    st = {x["name"]: x["status"] for x in res}
    assert st == {"escritura.pdf": "formato_valido", "falso.pdf": "formato_invalido", "copia.pdf": "duplicado"}
    assert next(x for x in res if x["name"] == "falso.pdf")["rejection"]["hint"]
    # sin tipo de proyecto no se envia
    assert public.post(f"/r/{token}/enviar", data={"applicant": "ACME", "project_type": ""}).status_code == 400
    assert calls == []
    assert public.post(f"/r/{token}/enviar", data={"applicant": "ACME", "project_type": "cedis"}).status_code == 200
    time.sleep(0.3)
    assert len(calls) == 1                                       # el Agente 1 se activo solo, sin accion del abogado
    assert public.get(f"/r/{token}").text.count("ya fue utilizado") == 1
    assert client.get("/panel").status_code == 200 and "ACME" in client.get("/panel").text


def test_archivos_cifrados_en_disco_y_purga_con_mfa(env):
    client, secret, calls, fs = env
    login(client, secret)
    token = client.post("/panel/enlaces", data={}).headers["location"].split("nuevo=")[1]
    pub = TestClient(client.app, follow_redirects=False)
    secreto = b"PROPIETARIO JUAN PEREZ TEXTO SECRETO"
    pub.post(f"/r/{token}/archivos", files=[("files", ("a.pdf", tiny_pdf(secreto.decode()), "application/pdf"))])
    store = fs.get_store()
    exp_id = store.list_ids()[0]
    d = store.enc.data_dir(exp_id)
    blobs = [p for p in d.rglob("*") if p.is_file()]
    assert blobs and all(p.suffix == ".enc" for p in blobs)
    assert all(b"JUAN PEREZ" not in p.read_bytes() for p in blobs)
    url = f"/panel/expedientes/{exp_id}/purgar"
    assert "Escriba" in client.post(url, data={"confirm": "no", "code": pyotp.TOTP(secret).now()}).headers["location"].replace("+", " ")
    assert "MFA" in client.post(url, data={"confirm": "PURGAR", "code": "123456"}).headers["location"]
    assert store.enc.has_key(exp_id)                             # nada se borro sin confirmacion + MFA
    r = client.post(url, data={"confirm": "PURGAR", "code": pyotp.TOTP(secret).now()})
    assert r.status_code == 200 and "Purga completada" in r.text
    assert not d.exists() and not store.enc.has_key(exp_id)
    assert client.get(f"/panel/expedientes/{exp_id}").status_code == 404
    from services.purge_service import receipts
    rc = receipts()[-1]
    assert rc["status"] == "completa" and rc["key_destroyed"] and "JUAN" not in str(rc)


def test_reglas_invalidas_no_se_guardan(env):
    client, secret, *_ = env
    login(client, secret)
    assert client.get("/panel/reglas").status_code == 200
    r = client.post("/panel/reglas", data={"text": "meta: {version: x}"})
    assert r.status_code == 400 and "errores de estructura" in r.text
