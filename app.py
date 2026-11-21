"""Cotejo - aplicacion web (FastAPI). Dos interfaces independientes pero conectadas:
  /r/{token}  formulario publico: solo 4 documentos para subir (quien vende o entrega los papeles)
  /panel/...  panel privado de Tiendas 3B (contrasena + codigo de verificacion)
Ejecutar:  uvicorn app:app --host 127.0.0.1 --port 8000"""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

import settings  # noqa: F401  (carga .env)
from services import mfa_service as auth
from services import presenter, rules_service
from services.encryption_service import MissingMasterKey
from services.form_service import get_store
from services.purge_service import purge_expediente

ROOT = Path(__file__).resolve().parent
@asynccontextmanager
async def _lifespan(_app):
    try:
        get_store().resume_pending()   # retoma analisis interrumpidos por un reinicio
    except Exception:
        pass
    yield


app = FastAPI(title="Cotejo", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))

SESSION_AGE = 8 * 3600
SLOTS = [("escrituras", "Escrituras", "Escritura o título de propiedad del inmueble"),
         ("predial", "Predial", "Recibo o boleta del impuesto predial"),
         ("alineamiento_numero_oficial", "Alineamiento y Número Oficial", "Constancia de alineamiento y número oficial"),
         ("uso_suelo", "Uso de Suelo", "Licencia o constancia de uso de suelo")]
SLOT_KEYS = {k for k, _, _ in SLOTS}
PROJECT_LABELS = {"store": "Tienda", "cedis": "Centro de distribución"}
STATE_LABEL = {"esperando": ("Esperando documentos", "gris"), "recibido": ("Analizando", "azul"), "en_analisis": ("Analizando", "azul"),
               "analizado": ("Listo", "verde"), "error": ("No se pudo analizar", "rojo")}
VERDICT_LABEL = {"cumple": ("Cumple", "verde"), "requiere_revision": ("Requiere revisión", "ambar"), "no_cumple": ("No cumple", "rojo")}
FILE_LABEL = {"formato_valido": "Recibido", "analizado": "Revisado", "formato_invalido": "No aceptado", "duplicado": "Repetido", "ilegible": "No se pudo leer"}


def _new_count() -> int:
    try:
        return sum(1 for m in get_store().list_metas() if m["status"] in ("recibido", "en_analisis", "analizado", "error") and not m.get("opened_at"))
    except Exception:
        return 0


templates.env.globals.update(STATE_LABEL=STATE_LABEL, VERDICT_LABEL=VERDICT_LABEL, PROJECT_LABELS=PROJECT_LABELS, FILE_LABEL=FILE_LABEL, SLOTS=SLOTS,
                             new_count=_new_count, PROGRESS_STEPS=presenter.PROGRESS_STEPS)


def _ser() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(os.environ.get("INMO_SESSION_SECRET", "x"), salt="panel-session")


def _session(request: Request) -> dict:
    try:
        return _ser().loads(request.cookies.get("inmo_session", ""), max_age=SESSION_AGE)
    except BadSignature:
        return {}


def lawyer(request: Request) -> str:
    s = _session(request)
    if s.get("stage") != "full":
        raise HTTPException(status_code=303, headers={"Location": "/panel/login"})
    return s["u"]


def _set_session(resp, username: str, stage: str):
    resp.set_cookie("inmo_session", _ser().dumps({"u": username, "stage": stage}), httponly=True, samesite="strict", max_age=SESSION_AGE)
    return resp


def _public_base(request: Request) -> str:
    """Direccion que se comparte con el cliente. Defina INMO_PUBLIC_URL si la app se publica con otro nombre o por internet."""
    return (os.environ.get("INMO_PUBLIC_URL") or str(request.base_url)).rstrip("/")


def _link(request: Request, exp_id: str) -> str:
    return f"{_public_base(request)}/r/{get_store().make_token(exp_id)}"


@app.exception_handler(MissingMasterKey)
async def _no_key(request: Request, exc: MissingMasterKey):
    return HTMLResponse(f"<h2>Configuración incompleta</h2><p>{exc}</p>", status_code=500)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/panel")


# =============================================================== FORMULARIO PUBLICO
def _intake_ctx(token: str):
    exp_id = get_store().resolve_token(token)
    if not exp_id:
        return None, None
    return exp_id, get_store().get_meta(exp_id)


@app.get("/r/{token}", response_class=HTMLResponse)
def intake_form(request: Request, token: str):
    exp_id, meta = _intake_ctx(token)
    if not exp_id:
        return templates.TemplateResponse(request, "intake_invalid.html", {}, status_code=404)
    if meta["status"] != "esperando":
        return templates.TemplateResponse(request, "intake_done.html", {"just_sent": False})
    return templates.TemplateResponse(request, "intake.html", {"token": token})


@app.post("/r/{token}/archivos")
async def intake_upload(token: str, slot: str = Form(""), files: list[UploadFile] = File(...)):
    exp_id, meta = _intake_ctx(token)
    if not exp_id:
        raise HTTPException(404, "Este enlace ya no está disponible.")
    items = [(f.filename or "archivo", await f.read()) for f in files]
    try:
        results = get_store().stage_files(exp_id, items, declared=slot if slot in SLOT_KEYS else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"results": [{"file_id": r["file_id"], "name": r["name"], "status": r["status"], "rejection": r["rejection"]} for r in results]}


@app.post("/r/{token}/quitar/{file_id}")
def intake_remove(token: str, file_id: str):
    exp_id, _ = _intake_ctx(token)
    if not exp_id:
        raise HTTPException(404)
    return {"removed": get_store().remove_file(exp_id, file_id)}


@app.post("/r/{token}/enviar")
def intake_submit(request: Request, token: str):
    exp_id, meta = _intake_ctx(token)
    if not exp_id:
        return templates.TemplateResponse(request, "intake_invalid.html", {}, status_code=404)
    try:
        get_store().submit(exp_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


@app.get("/r/{token}/gracias", response_class=HTMLResponse)
def intake_thanks(request: Request, token: str):
    return templates.TemplateResponse(request, "intake_done.html", {"just_sent": True})


# ======================================================================= ACCESO
@app.get("/panel/login", response_class=HTMLResponse)
def login_page(request: Request):
    s = _session(request)
    return templates.TemplateResponse(request, "login.html", {"stage": s.get("stage", "none"), "has_users": auth.has_users(), "error": None})


@app.post("/panel/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if auth.is_locked(username):
        return templates.TemplateResponse(request, "login.html", {"stage": "none", "has_users": True, "error": "Demasiados intentos. Espere unos minutos e intente de nuevo."}, status_code=429)
    if not auth.verify_password(username, password):
        return templates.TemplateResponse(request, "login.html", {"stage": "none", "has_users": auth.has_users(), "error": "El usuario o la contraseña no son correctos."}, status_code=401)
    return _set_session(templates.TemplateResponse(request, "login.html", {"stage": "pwd", "has_users": True, "error": None}), username, "pwd")


@app.post("/panel/mfa")
def mfa(request: Request, code: str = Form(...)):
    s = _session(request)
    if s.get("stage") not in ("pwd", "full") or not s.get("u"):
        return RedirectResponse("/panel/login", status_code=303)
    if not auth.verify_totp(s["u"], code):
        return templates.TemplateResponse(request, "login.html", {"stage": "pwd", "has_users": True, "error": "El código no es correcto o ya venció."}, status_code=401)
    return _set_session(RedirectResponse("/panel", status_code=303), s["u"], "full")


@app.get("/panel/logout")
def logout():
    r = RedirectResponse("/panel/login", status_code=303)
    r.delete_cookie("inmo_session")
    return r


# ======================================================================= PANEL
def _row(m: dict) -> dict:
    slots_ok = {f.get("declared") for f in m["files"] if f["status"] in ("formato_valido", "analizado", "ilegible") and f.get("declared")}
    state, color = STATE_LABEL.get(m["status"], (m["status"], "gris"))
    v = VERDICT_LABEL.get(m.get("verdict"))
    return {**m, "state": state, "state_color": color, "verdict_label": v[0] if v else None, "verdict_color": v[1] if v else None, "docs_ok": len(slots_ok),
            "is_new": m["status"] in ("recibido", "en_analisis", "analizado", "error") and not m.get("opened_at"), "project_label": PROJECT_LABELS.get(m.get("project_type"), "—")}


@app.get("/panel", response_class=HTMLResponse)
def home(request: Request, user: str = Depends(lawyer)):
    rows = [_row(m) for m in get_store().list_metas()]
    c = {"nuevas": sum(r["is_new"] for r in rows), "analizando": sum(r["status"] in ("recibido", "en_analisis") for r in rows),
         "revisar": sum(r.get("verdict") == "requiere_revision" for r in rows), "cumplen": sum(r.get("verdict") == "cumple" for r in rows),
         "no_cumplen": sum(r.get("verdict") == "no_cumple" for r in rows), "esperando": sum(r["status"] == "esperando" for r in rows)}
    return templates.TemplateResponse(request, "home.html", {"user": user, "nav": "inicio", "c": c, "new": [r for r in rows if r["is_new"]][:6], "recent": rows[:6], "total": len(rows)})


@app.get("/panel/nuevo", response_class=HTMLResponse)
def link_new(request: Request, user: str = Depends(lawyer), error: str = ""):
    return templates.TemplateResponse(request, "link_new.html", {"user": user, "nav": "nuevo", "error": error})


@app.post("/panel/nuevo")
def link_create(user: str = Depends(lawyer), applicant: str = Form(""), project_type: str = Form("")):
    try:
        exp_id, _ = get_store().create_link(applicant, project_type)
    except ValueError as exc:
        from urllib.parse import quote
        return RedirectResponse(f"/panel/nuevo?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/panel/enlaces/{exp_id}", status_code=303)


@app.get("/panel/enlaces/{exp_id}", response_class=HTMLResponse)
def link_ready(request: Request, exp_id: str, user: str = Depends(lawyer)):
    store = get_store()
    if not store.enc.has_key(exp_id):
        raise HTTPException(404, "Expediente no encontrado.")
    meta = store.get_meta(exp_id)
    link = _link(request, exp_id)
    from urllib.parse import quote
    wa = "https://wa.me/?text=" + quote(f"Hola, para continuar con la revisión de su inmueble, suba sus documentos en este enlace: {link}")
    return templates.TemplateResponse(request, "link_ready.html", {"user": user, "nav": "nuevo", "meta": meta, "link": link, "wa": wa})


@app.get("/panel/qr/{exp_id}.png")
def qr_png(request: Request, exp_id: str, user: str = Depends(lawyer)):
    if not get_store().enc.has_key(exp_id):
        raise HTTPException(404)
    return Response(base64.b64decode(auth.qr_png_b64(_link(request, exp_id))), media_type="image/png")


@app.get("/panel/expedientes", response_class=HTMLResponse)
def expedientes(request: Request, user: str = Depends(lawyer), q: str = "", f: str = ""):
    rows = [_row(m) for m in get_store().list_metas()]

    def match(r) -> bool:
        if q and q.lower() not in (r["exp_id"] + r.get("applicant", "") + r.get("reference", "")).lower():
            return False
        return {"": True, "nuevas": r["is_new"], "analizando": r["status"] in ("recibido", "en_analisis"), "esperando": r["status"] == "esperando",
                "cumple": r.get("verdict") == "cumple", "revisar": r.get("verdict") == "requiere_revision", "no_cumple": r.get("verdict") == "no_cumple"}.get(f, True)
    return templates.TemplateResponse(request, "expedientes.html", {"user": user, "nav": "expedientes", "rows": [r for r in rows if match(r)], "q": q, "f": f, "total": len(rows)})


SECTIONS = [("resumen", "Resumen"), ("observaciones", "Observaciones"), ("documentos", "Documentos"), ("requisitos", "Requisitos"), ("datos", "Datos del inmueble"),
            ("eliminar", "Eliminar")]


@app.get("/panel/expedientes/{exp_id}", response_class=HTMLResponse)
def detail(request: Request, exp_id: str, user: str = Depends(lawyer), s: str = "resumen", error: str = ""):
    store = get_store()
    if not store.enc.has_key(exp_id):
        raise HTTPException(404, "Este expediente no existe o ya fue eliminado.")
    store.mark_opened(exp_id)
    meta = store.get_meta(exp_id)
    result = store.get_result(exp_id)
    plain = presenter.build(result, meta) if result else None
    if s not in {k for k, _ in SECTIONS}:
        s = "resumen"
    state, color = STATE_LABEL.get(meta["status"], (meta["status"], "gris"))
    files = [{**f, "label": FILE_LABEL.get(f["status"], f["status"]), "slot": next((t for k, t, _ in SLOTS if k == f.get("declared")), "—")} for f in meta["files"]]
    return templates.TemplateResponse(request, "detail.html", {"user": user, "nav": "expedientes", "meta": meta, "p": plain, "section": s, "sections": SECTIONS, "state": state, "state_color": color,
                                                               "files": files, "error": error, "link": _link(request, exp_id) if meta["status"] == "esperando" else None,
                                                               "timing": round(result["timings_ms"]["total"] / 1000) if result else None})


@app.get("/panel/expedientes/{exp_id}/estado.json")
def status_json(exp_id: str, user: str = Depends(lawyer)):
    store = get_store()
    if not store.enc.has_key(exp_id):
        return JSONResponse({"status": "purgado", "step": 0})
    meta = store.get_meta(exp_id)
    prog = store.progress(exp_id) or meta.get("progress", [])
    step = max([presenter.progress_step(p["stage"]) for p in prog] or [0])
    return {"status": meta["status"], "step": step if meta["status"] != "analizado" else len(presenter.PROGRESS_STEPS)}


@app.post("/panel/expedientes/{exp_id}/eliminar")
def purge(request: Request, exp_id: str, user: str = Depends(lawyer), confirm: str = Form("")):
    from urllib.parse import quote
    # La sesion ya paso contrasena + codigo; aqui basta la confirmacion explicita.
    if confirm != "ELIMINAR":
        return RedirectResponse(f"/panel/expedientes/{exp_id}?s=eliminar&error={quote('Confirma que quieres eliminarlo.')}", status_code=303)
    receipt = purge_expediente(get_store().enc, exp_id)
    return templates.TemplateResponse(request, "purged.html", {"user": user, "nav": "expedientes", "receipt": receipt})


# Administracion de requisitos: sin enlace en el menu (no es parte del uso diario); solo con sesion verificada.
@app.get("/panel/reglas", response_class=HTMLResponse)
def rules_page(request: Request, user: str = Depends(lawyer), msg: str = "", err: str = ""):
    loaded = rules_service.load_rules()
    return templates.TemplateResponse(request, "rules.html", {"user": user, "nav": "", "ref": loaded.reference(), "text": Path(loaded.path).read_text(encoding="utf-8"),
                                                              "history": rules_service.rules_history()[::-1], "msg": msg, "err": err})


@app.post("/panel/reglas", response_class=HTMLResponse)
def rules_update(request: Request, user: str = Depends(lawyer), text: str = Form(...)):
    try:
        rules_service.update_rules(text.replace("\r\n", "\n"), actor=user)
    except rules_service.RulesError as exc:
        loaded = rules_service.load_rules()
        return templates.TemplateResponse(request, "rules.html", {"user": user, "nav": "", "ref": loaded.reference(), "text": text, "history": rules_service.rules_history()[::-1], "msg": "", "err": str(exc)}, status_code=400)
    return RedirectResponse("/panel/reglas?msg=Reglas+actualizadas", status_code=303)
