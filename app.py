"""InmoVeritas - aplicacion web (FastAPI). Dos interfaces independientes pero conectadas:
  /r/{token}  formulario publico de recepcion (la contraparte)         -> activa al Agente 1 al enviarse
  /panel/...  panel privado del abogado (contrasena + TOTP)
Ejecutar:  uvicorn app:app --host 127.0.0.1 --port 8000"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

import settings  # noqa: F401  (carga .env)
from schemas.schemas import DOC_TYPE_LABELS, PROJECT_LABELS, DocType, ProjectType
from services import mfa_service as auth
from services import rules_service
from services.encryption_service import MissingMasterKey
from services.form_service import get_store
from services.purge_service import purge_expediente, receipts

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="InmoVeritas", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))

SESSION_AGE = 8 * 3600
VERDICT_LABEL = {"cumple": "Cumple", "requiere_revision": "Requiere revisión", "no_cumple": "No cumple", None: "Sin recomendación"}
STATUS_LABEL = {"esperando": "Esperando documentos", "recibido": "Recibido", "en_analisis": "En análisis", "analizado": "Analizado", "error": "Error en el análisis"}
FILE_STATUS_LABEL = {"recibido": "Recibido", "formato_valido": "Formato válido", "formato_invalido": "Formato inválido", "duplicado": "Duplicado",
                     "ilegible": "Ilegible", "en_procesamiento": "En procesamiento", "analizado": "Analizado", "pendiente_revision": "Pendiente de revisión"}
REQ_LABEL = {"cumplido": "Cumple", "incumplido": "No cumple", "pendiente": "Pendiente de revisión"}
templates.env.globals.update(VERDICT_LABEL=VERDICT_LABEL, STATUS_LABEL=STATUS_LABEL, FILE_STATUS_LABEL=FILE_STATUS_LABEL, REQ_LABEL=REQ_LABEL,
                             PROJECT_LABELS={k.value: v for k, v in PROJECT_LABELS.items()}, DOC_LABELS={k.value: v for k, v in DOC_TYPE_LABELS.items()})


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
        return templates.TemplateResponse(request, "intake_done.html", {"meta": meta})
    return templates.TemplateResponse(request, "intake.html", {"token": token, "meta": meta})


@app.post("/r/{token}/archivos")
async def intake_upload(token: str, files: list[UploadFile] = File(...)):
    exp_id, meta = _intake_ctx(token)
    if not exp_id:
        raise HTTPException(404, "Enlace no válido o vencido.")
    items = [(f.filename or "archivo", await f.read()) for f in files]
    try:
        results = get_store().stage_files(exp_id, items)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"results": results}


@app.post("/r/{token}/quitar/{file_id}")
def intake_remove(token: str, file_id: str):
    exp_id, _ = _intake_ctx(token)
    if not exp_id:
        raise HTTPException(404)
    return {"removed": get_store().remove_file(exp_id, file_id)}


@app.post("/r/{token}/enviar", response_class=HTMLResponse)
def intake_submit(request: Request, token: str, applicant: str = Form(""), project_type: str = Form("")):
    exp_id, meta = _intake_ctx(token)
    if not exp_id:
        return templates.TemplateResponse(request, "intake_invalid.html", {}, status_code=404)
    try:
        meta = get_store().submit(exp_id, applicant, project_type)
    except ValueError as exc:
        meta = get_store().get_meta(exp_id)
        return templates.TemplateResponse(request, "intake.html", {"token": token, "meta": meta, "error": str(exc)}, status_code=400)
    return templates.TemplateResponse(request, "intake_done.html", {"meta": meta, "just_sent": True})


# ======================================================================= PANEL
@app.get("/panel/login", response_class=HTMLResponse)
def login_page(request: Request):
    s = _session(request)
    return templates.TemplateResponse(request, "login.html", {"stage": s.get("stage", "none"), "has_users": auth.has_users(), "error": None})


@app.post("/panel/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if auth.is_locked(username):
        return templates.TemplateResponse(request, "login.html", {"stage": "none", "has_users": True, "error": "Demasiados intentos fallidos. Espere unos minutos."}, status_code=429)
    if not auth.verify_password(username, password):
        return templates.TemplateResponse(request, "login.html", {"stage": "none", "has_users": auth.has_users(), "error": "Usuario o contraseña incorrectos."}, status_code=401)
    return _set_session(templates.TemplateResponse(request, "login.html", {"stage": "pwd", "has_users": True, "error": None}), username, "pwd")


@app.post("/panel/mfa")
def mfa(request: Request, code: str = Form(...)):
    s = _session(request)
    if s.get("stage") not in ("pwd", "full") or not s.get("u"):
        return RedirectResponse("/panel/login", status_code=303)
    if not auth.verify_totp(s["u"], code):
        return templates.TemplateResponse(request, "login.html", {"stage": "pwd", "has_users": True, "error": "Código incorrecto o vencido."}, status_code=401)
    return _set_session(RedirectResponse("/panel", status_code=303), s["u"], "full")


@app.get("/panel/logout")
def logout():
    r = RedirectResponse("/panel/login", status_code=303)
    r.delete_cookie("inmo_session")
    return r


def _counts(metas: list[dict]) -> dict:
    c = {"total": len(metas), "en_analisis": 0, "analizados": 0, "invalidos": 0, "ilegibles": 0, "revision": 0, "cumple": 0, "no_cumple": 0}
    for m in metas:
        c["en_analisis"] += m["status"] in ("recibido", "en_analisis")
        c["analizados"] += m["status"] == "analizado"
        c["invalidos"] += any(f["status"] == "formato_invalido" for f in m["files"])
        c["ilegibles"] += any(f["status"] == "ilegible" for f in m["files"])
        c["revision"] += m.get("verdict") == "requiere_revision"
        c["cumple"] += m.get("verdict") == "cumple"
        c["no_cumple"] += m.get("verdict") == "no_cumple"
    return c


@app.get("/panel", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(lawyer), q: str = "", estado: str = "", nuevo: str = ""):
    metas = get_store().list_metas()
    counts = _counts(metas)
    shown = [m for m in metas if (not q or q.lower() in (m["exp_id"] + m.get("applicant", "") + m.get("reference", "")).lower())
             and (not estado or m["status"] == estado or m.get("verdict") == estado)]
    rows = []
    for m in shown:
        fs = m["files"]
        rows.append({**m, "n_docs": len(fs), "n_valid": sum(f["status"] in ("formato_valido", "analizado", "ilegible") for f in fs),
                     "n_rej": sum(f["status"] in ("formato_invalido", "duplicado") for f in fs), "n_ileg": sum(f["status"] == "ilegible" for f in fs)})
    link = None
    if nuevo:
        link = str(request.base_url).rstrip("/") + "/r/" + nuevo
    return templates.TemplateResponse(request, "dashboard.html", {"user": user, "counts": counts, "rows": rows, "q": q, "estado": estado, "link": link,
                                                                   "purges": receipts()[-5:][::-1], "rules": rules_service.load_rules().reference()})


@app.post("/panel/enlaces")
def create_link(user: str = Depends(lawyer), reference: str = Form("")):
    exp_id, token = get_store().create_link(reference)
    return RedirectResponse(f"/panel?nuevo={token}", status_code=303)


def _security(exp_id: str, session_ok: bool) -> list[dict]:
    store = get_store()
    enc = store.enc
    d = enc.data_dir(exp_id)
    files = [p for p in d.rglob("*") if p.is_file()] if d.exists() else []
    plain = [p for p in files if p.suffix != ".enc"]
    have_key = enc.has_key(exp_id)
    ok_enc = have_key and bool(files) and not plain
    meta = store.get_meta(exp_id) if have_key else {}
    return [
        {"name": "Cifrado AES-256-GCM", "ok": ok_enc, "detail": f"{len(files)} archivo(s) cifrado(s); sin archivos en texto plano" if ok_enc else "No verificado"},
        {"name": "Autenticación MFA", "ok": session_ok, "detail": "Sesión con contraseña + TOTP verificados" if session_ok else "Sin sesión verificada"},
        {"name": "Protección del expediente", "ok": have_key, "detail": "Clave propia del expediente, separada de los archivos" if have_key else "Clave no disponible"},
        {"name": "Procesamiento", "ok": meta.get("status") == "analizado", "detail": STATUS_LABEL.get(meta.get("status"), "—")},
        {"name": "Almacenamiento", "ok": bool(files) and not plain, "detail": "Almacenamiento local cifrado (no público)" if files else "Sin datos"},
        {"name": "Eliminación", "ok": False, "detail": "No purgado (la purga está disponible con confirmación y MFA)"},
    ]


@app.get("/panel/expedientes/{exp_id}", response_class=HTMLResponse)
def detail(request: Request, exp_id: str, user: str = Depends(lawyer), purgado: str = ""):
    store = get_store()
    if not store.enc.has_key(exp_id):
        raise HTTPException(404, "Expediente no encontrado o purgado.")
    meta = store.get_meta(exp_id)
    result = store.get_result(exp_id)
    ev_by_file = {}
    if result:
        for rq in result["requirements"]:
            pass
    return templates.TemplateResponse(request, "detail.html", {"user": user, "meta": meta, "r": result, "security": _security(exp_id, True),
                                                               "progress": store.progress(exp_id), "error": request.query_params.get("error")})


@app.get("/panel/expedientes/{exp_id}/estado.json")
def status_json(exp_id: str, user: str = Depends(lawyer)):
    store = get_store()
    if not store.enc.has_key(exp_id):
        return JSONResponse({"status": "purgado"})
    meta = store.get_meta(exp_id)
    return {"status": meta["status"], "progress": store.progress(exp_id) or meta.get("progress", []), "verdict": meta.get("verdict")}


@app.post("/panel/expedientes/{exp_id}/purgar")
def purge(request: Request, exp_id: str, user: str = Depends(lawyer), code: str = Form(""), confirm: str = Form("")):
    if confirm.strip().upper() != "PURGAR":
        return RedirectResponse(f"/panel/expedientes/{exp_id}?error=Escriba+PURGAR+para+confirmar", status_code=303)
    if not auth.verify_totp(user, code):
        return RedirectResponse(f"/panel/expedientes/{exp_id}?error=Código+MFA+incorrecto", status_code=303)
    receipt = purge_expediente(get_store().enc, exp_id)
    return templates.TemplateResponse(request, "purged.html", {"receipt": receipt})


@app.get("/panel/reglas", response_class=HTMLResponse)
def rules_page(request: Request, user: str = Depends(lawyer), msg: str = "", err: str = ""):
    loaded = rules_service.load_rules()
    return templates.TemplateResponse(request, "rules.html", {"user": user, "ref": loaded.reference(), "text": Path(loaded.path).read_text(encoding="utf-8"),
                                                              "history": rules_service.rules_history()[::-1], "msg": msg, "err": err})


@app.post("/panel/reglas", response_class=HTMLResponse)
def rules_update(request: Request, user: str = Depends(lawyer), text: str = Form(...)):
    try:
        rules_service.update_rules(text.replace("\r\n", "\n"), actor=user)
    except rules_service.RulesError as exc:
        loaded = rules_service.load_rules()
        return templates.TemplateResponse(request, "rules.html", {"user": user, "ref": loaded.reference(), "text": text, "history": rules_service.rules_history()[::-1], "msg": "", "err": str(exc)}, status_code=400)
    return RedirectResponse("/panel/reglas?msg=Reglas+actualizadas", status_code=303)
