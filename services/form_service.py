"""Expedientes: enlace de recepcion, carga validada y cifrada, evento 'expediente recibido' y analisis automatico.

Todo lo que se guarda va cifrado por expediente (AES-256-GCM): archivos originales, copias procesadas, metadatos y resultados.
El envio del formulario publica un evento que activa AUTOMATICAMENTE al Agente 1 (sin accion manual del abogado)."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from schemas.schemas import FileStatus, ProjectType
from services.encryption_service import EncryptionService, KeyDestroyed, get_encryption_service
from services.file_validator import validate_submission
from services.rules_service import load_rules
from settings import get_settings

import os

LINK_MAX_AGE = 7 * 24 * 3600
_progress: dict[str, list[dict]] = {}
_handlers: dict[str, list[Callable]] = {"expediente_recibido": []}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("INMO_SESSION_SECRET", "")
    if not secret:
        raise RuntimeError("Falta INMO_SESSION_SECRET (ejecute scripts/setup_env.py).")
    return URLSafeTimedSerializer(secret, salt="intake-link")


# -------------------------------------------------------------------- eventos
def subscribe(event: str, handler: Callable) -> None:
    _handlers.setdefault(event, []).append(handler)


def publish(event: str, **payload) -> None:
    for h in _handlers.get(event, []):
        threading.Thread(target=h, kwargs=payload, daemon=True).start()


# ------------------------------------------------------------------ expedientes
class ExpedienteStore:
    def __init__(self, enc: Optional[EncryptionService] = None):
        self.enc = enc or get_encryption_service()

    # metadatos (cifrados)
    def get_meta(self, exp_id: str) -> dict:
        return self.enc.get_json(exp_id, "meta", "meta")

    def put_meta(self, exp_id: str, meta: dict) -> None:
        with _lock:
            self.enc.put_json(exp_id, "meta", "meta", meta)

    def list_ids(self) -> list[str]:
        d = self.enc.keys_dir()
        return sorted(p.name[:-9] for p in d.glob("*.key.json")) if d.exists() else []

    def list_metas(self) -> list[dict]:
        out = []
        for eid in self.list_ids():
            try:
                out.append(self.get_meta(eid))
            except Exception:
                continue
        return sorted(out, key=lambda m: m.get("created_at", ""), reverse=True)

    # enlace
    def create_link(self, reference: str = "") -> tuple[str, str]:
        exp_id = "EXP-" + uuid.uuid4().hex[:8].upper()
        self.enc.create_expediente_key(exp_id)
        self.put_meta(exp_id, {"exp_id": exp_id, "reference": reference.strip()[:80], "applicant": "", "project_type": None, "created_at": _now(),
                               "status": "esperando", "files": [], "progress": [], "verdict": None, "confidence": None})
        return exp_id, _serializer().dumps({"e": exp_id})

    def resolve_token(self, token: str) -> Optional[str]:
        try:
            exp_id = _serializer().loads(token, max_age=LINK_MAX_AGE)["e"]
        except (BadSignature, SignatureExpired):
            return None
        return exp_id if self.enc.has_key(exp_id) else None

    # carga validada
    def stage_files(self, exp_id: str, items: list[tuple[str, bytes]]) -> list[dict]:
        meta = self.get_meta(exp_id)
        if meta["status"] not in ("esperando",):
            raise ValueError("Este expediente ya fue enviado; no admite más archivos.")
        existing = {f["sha256"]: f["file_id"] for f in meta["files"] if f["status"] == FileStatus.FORMATO_VALIDO.value}
        accepted_count = len(existing)
        results = validate_submission(items)
        out = []
        for r in results:
            rec = {"file_id": r.file_id, "name": r.original_name, "source": r.source, "format": r.detected_format, "size": r.size_bytes, "sha256": r.sha256,
                   "status": r.status.value, "rejection": r.rejection.model_dump() if r.rejection else None, "warnings": r.warnings, "pages": r.pages, "duplicate_of": r.duplicate_of}
            if r.status == FileStatus.FORMATO_VALIDO:
                if r.sha256 in existing:  # duplicado de un archivo ya cargado antes
                    rec.update(status=FileStatus.DUPLICADO.value, duplicate_of=existing[r.sha256],
                               rejection={"code": "duplicado", "message": f"«{r.original_name}» es idéntico a un archivo ya recibido.", "hint": "No necesita volver a subirlo."})
                elif accepted_count >= get_settings().max_files_per_submission:
                    rec.update(status=FileStatus.FORMATO_INVALIDO.value, rejection={"code": "limite_archivos", "message": "Se alcanzó el máximo de archivos por envío.", "hint": "Envíe el resto en otro expediente."})
                else:
                    self.enc.put(exp_id, "original", r.file_id, r.data)
                    existing[r.sha256] = r.file_id
                    accepted_count += 1
            meta["files"].append(rec)
            out.append(rec)
        self.put_meta(exp_id, meta)
        return out

    def remove_file(self, exp_id: str, file_id: str) -> bool:
        meta = self.get_meta(exp_id)
        if meta["status"] != "esperando":
            return False
        meta["files"] = [f for f in meta["files"] if f["file_id"] != file_id]
        p = self.enc.data_dir(exp_id) / "original" / f"{file_id}.enc"
        if p.exists():
            p.unlink()
        self.put_meta(exp_id, meta)
        return True

    def submit(self, exp_id: str, applicant: str, project_type: str) -> dict:
        meta = self.get_meta(exp_id)
        if meta["status"] != "esperando":
            raise ValueError("Este expediente ya fue enviado.")
        if project_type not in ("store", "cedis"):
            raise ValueError("Seleccione el tipo de proyecto: tienda comercial o CEDIS.")
        if not any(f["status"] == FileStatus.FORMATO_VALIDO.value for f in meta["files"]):
            raise ValueError("Adjunte al menos un archivo válido antes de enviar.")
        meta.update(applicant=applicant.strip()[:120], project_type=project_type, status="recibido", submitted_at=_now())
        self.put_meta(exp_id, meta)
        publish("expediente_recibido", exp_id=exp_id)   # activa al Agente 1 automaticamente
        return meta

    # analisis
    def _progress(self, exp_id: str):
        t0 = time.monotonic()

        def cb(stage: str, detail: str = ""):
            ev = {"stage": stage, "detail": detail, "t": round(time.monotonic() - t0, 1)}
            _progress.setdefault(exp_id, []).append(ev)
        return cb

    def run_analysis(self, exp_id: str) -> None:
        from agents.coordinador import CoordinadorAgent  # import tardio: carga LangChain/OCR solo cuando hace falta
        try:
            meta = self.get_meta(exp_id)
            meta["status"] = "en_analisis"
            self.put_meta(exp_id, meta)
            _progress[exp_id] = []
            files = []
            for f in meta["files"]:
                if f["status"] == FileStatus.FORMATO_VALIDO.value:
                    files.append({"file_id": f["file_id"], "name": f["name"], "fmt": f["format"], "data": self.enc.get(exp_id, "original", f["file_id"])})
            rules = load_rules()
            coord = CoordinadorAgent(progress=self._progress(exp_id))
            result, pngs = coord.analyze(exp_id, files, ProjectType(meta["project_type"]), rules)
            for fid, per_page in pngs.items():
                for page, png in (per_page or {}).items():
                    self.enc.put(exp_id, "procesado", f"{fid}_p{page}", png)
            self.enc.put_json(exp_id, "resultado", "analisis", result)
            meta = self.get_meta(exp_id)
            unreadable = {d["file_id"] for d in result["documents"] if d["unreadable_pages"] or not d["has_text"]}
            for f in meta["files"]:
                if f["file_id"] in unreadable:
                    f["status"] = FileStatus.ILEGIBLE.value
                elif f["status"] == FileStatus.FORMATO_VALIDO.value:
                    f["status"] = FileStatus.ANALIZADO.value
            meta.update(status="analizado", verdict=result["recommendation"]["verdict"], confidence=result["recommendation"]["confidence"],
                        analyzed_at=_now(), total_ms=result["timings_ms"]["total"], progress=_progress.get(exp_id, []))
            self.put_meta(exp_id, meta)
        except KeyDestroyed:
            return
        except Exception as exc:
            try:
                meta = self.get_meta(exp_id)
                meta.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:200]}")
                self.put_meta(exp_id, meta)
            except Exception:
                pass

    def progress(self, exp_id: str) -> list[dict]:
        return _progress.get(exp_id, [])

    def get_result(self, exp_id: str) -> Optional[dict]:
        try:
            return self.enc.get_json(exp_id, "resultado", "analisis")
        except FileNotFoundError:
            return None


_store: Optional[ExpedienteStore] = None


def get_store() -> ExpedienteStore:
    global _store
    if _store is None:
        _store = ExpedienteStore()
        subscribe("expediente_recibido", lambda exp_id: _store.run_analysis(exp_id))
    return _store
