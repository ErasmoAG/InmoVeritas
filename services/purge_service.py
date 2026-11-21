"""Purga y destruccion controlada de un expediente.

Elimina archivos originales, copias procesadas, temporales, resultados, metadatos y DESTRUYE la clave de cifrado.
Registra una constancia sin datos personales ni contenido. Informa que quedo pendiente (si algo). NO afirma
Zero-Retention absoluto: pueden existir copias en respaldos o almacenamiento externo fuera del control de la aplicacion."""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone

from services.encryption_service import EncryptionService
from settings import get_settings


def _receipts_path():
    return get_settings().storage_dir / "purge_receipts.jsonl"


def purge_expediente(enc: EncryptionService, exp_id: str) -> dict:
    data_dir = enc.data_dir(exp_id)
    n_files = sum(1 for p in data_dir.rglob("*") if p.is_file()) if data_dir.exists() else 0
    pending: list[str] = []
    if data_dir.exists():
        try:
            shutil.rmtree(data_dir)
        except OSError as exc:
            pending.append(f"No se pudieron borrar algunos archivos del expediente ({type(exc).__name__}).")
    key_destroyed = enc.destroy_key(exp_id)
    if not key_destroyed:
        pending.append("La clave de cifrado no pudo destruirse.")
    leftover = data_dir.exists() and any(data_dir.rglob("*"))
    if leftover:
        pending.append("Quedan archivos cifrados en disco (irrecuperables solo si la clave fue destruida).")
    complete = not pending and not data_dir.exists() and not enc.has_key(exp_id)
    receipt = {"purge_id": uuid.uuid4().hex[:12], "expediente": exp_id, "purged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "files_deleted": n_files, "key_destroyed": key_destroyed, "status": "completa" if complete else "incompleta", "pending": pending,
               "notice": "No se garantiza la eliminación de copias fuera del control de la aplicación (respaldos, almacenamiento externo, registros del sistema)."}
    p = _receipts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    return receipt


def receipts() -> list[dict]:
    p = _receipts_path()
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []
