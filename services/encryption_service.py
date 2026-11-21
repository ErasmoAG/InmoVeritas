"""Cifrado AES-256-GCM por expediente (biblioteca `cryptography`).

Modelo de claves (MVP):
  - Clave maestra (32 bytes, base64) en la variable de entorno INMO_MASTER_KEY.
  - Cada expediente tiene su propia clave aleatoria de 32 bytes.
  - La clave del expediente se guarda ENVUELTA (cifrada con la maestra, AES-GCM) en
    storage/keys/<expediente>.key.json, en un directorio distinto al de los archivos cifrados.
  - Cada operacion usa un nonce aleatorio de 96 bits (os.urandom), nunca reutilizado a proposito.
  - AAD (datos asociados) liga cada blob a su expediente y a su rol, para que no se pueda
    mover un archivo cifrado de un expediente a otro sin que falle la autenticacion.
  - El codigo TOTP NO se usa como clave y las claves NUNCA se escriben en logs.

Limitaciones del MVP (documentadas en el README): la clave maestra vive en el entorno
local y las claves envueltas estan en el mismo disco. No es un KMS ni un HSM.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from settings import get_settings

MAGIC = b"IVE1"          # version del formato del blob
NONCE_LEN = 12
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class EncryptionError(Exception):
    pass


class MissingMasterKey(EncryptionError):
    pass


class KeyDestroyed(EncryptionError):
    """La clave del expediente fue destruida o nunca existio: los datos no son recuperables."""


class TamperedData(EncryptionError):
    """El contenido cifrado no autentica (alterado, otra clave o AAD distinta)."""


def generate_master_key() -> str:
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode("ascii")


def _check_id(value: str, what: str) -> str:
    if not _ID_RE.match(value or ""):
        raise EncryptionError(f"Identificador de {what} inválido.")
    return value


def _load_master() -> bytes:
    raw = os.environ.get("INMO_MASTER_KEY", "").strip()
    if not raw:
        raise MissingMasterKey("Falta INMO_MASTER_KEY. Genere una con: python scripts/setup_env.py")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise MissingMasterKey("INMO_MASTER_KEY no es base64 válido.") from exc
    if len(key) != 32:
        raise MissingMasterKey("INMO_MASTER_KEY debe contener exactamente 32 bytes (AES-256).")
    return key


class EncryptionService:
    def __init__(self, storage_dir: Optional[Path] = None, master_key: Optional[bytes] = None):
        self.storage_dir = Path(storage_dir) if storage_dir else get_settings().storage_dir
        self._master = master_key if master_key is not None else _load_master()
        if len(self._master) != 32:
            raise MissingMasterKey("La clave maestra debe tener 32 bytes.")
        self._cache: dict[str, bytes] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ rutas
    def keys_dir(self) -> Path:
        return self.storage_dir / "keys"

    def key_path(self, exp_id: str) -> Path:
        return self.keys_dir() / f"{_check_id(exp_id, 'expediente')}.key.json"

    def data_dir(self, exp_id: str) -> Path:
        return self.storage_dir / "expedientes" / _check_id(exp_id, "expediente")

    # ----------------------------------------------------------- claves de expediente
    def create_expediente_key(self, exp_id: str) -> None:
        path = self.key_path(exp_id)
        with self._lock:
            if path.exists():
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            dek = AESGCM.generate_key(bit_length=256)
            nonce = os.urandom(NONCE_LEN)
            wrapped = AESGCM(self._master).encrypt(nonce, dek, f"dek:{exp_id}".encode())
            path.write_text(json.dumps({"v": 1, "nonce": base64.b64encode(nonce).decode(),
                                        "wrapped": base64.b64encode(wrapped).decode()}), encoding="utf-8")
            self._cache[exp_id] = dek

    def _dek(self, exp_id: str) -> bytes:
        with self._lock:
            if exp_id in self._cache:
                return self._cache[exp_id]
        path = self.key_path(exp_id)
        if not path.exists():
            raise KeyDestroyed("La clave de este expediente no existe o fue destruida.")
        meta = json.loads(path.read_text(encoding="utf-8"))
        try:
            dek = AESGCM(self._master).decrypt(base64.b64decode(meta["nonce"]), base64.b64decode(meta["wrapped"]),
                                               f"dek:{exp_id}".encode())
        except InvalidTag as exc:
            raise TamperedData("No se pudo desenvolver la clave del expediente (¿clave maestra distinta?).") from exc
        with self._lock:
            self._cache[exp_id] = dek
        return dek

    def has_key(self, exp_id: str) -> bool:
        return self.key_path(exp_id).exists()

    def destroy_key(self, exp_id: str) -> bool:
        """Sobrescribe y borra la clave envuelta y la saca de memoria.
        Devuelve True si no queda rastro de la clave en el directorio de claves."""
        path = self.key_path(exp_id)
        with self._lock:
            self._cache.pop(exp_id, None)
        if path.exists():
            try:
                size = path.stat().st_size
                with path.open("r+b") as fh:
                    fh.write(os.urandom(max(size, 64)))
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                pass
            path.unlink()
        return not path.exists()

    # --------------------------------------------------------------- cifrado basico
    @staticmethod
    def _aad(exp_id: str, role: str) -> bytes:
        return f"{exp_id}|{role}".encode()

    def encrypt(self, exp_id: str, plaintext: bytes, role: str) -> bytes:
        nonce = os.urandom(NONCE_LEN)
        ct = AESGCM(self._dek(exp_id)).encrypt(nonce, plaintext, self._aad(exp_id, role))
        return MAGIC + nonce + ct

    def decrypt(self, exp_id: str, blob: bytes, role: str) -> bytes:
        if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + NONCE_LEN + 16:
            raise TamperedData("Formato de archivo cifrado no reconocido.")
        nonce = blob[len(MAGIC):len(MAGIC) + NONCE_LEN]
        ct = blob[len(MAGIC) + NONCE_LEN:]
        try:
            return AESGCM(self._dek(exp_id)).decrypt(nonce, ct, self._aad(exp_id, role))
        except InvalidTag as exc:
            raise TamperedData("El archivo cifrado fue alterado o no corresponde a este expediente.") from exc

    # ------------------------------------------------------------- almacenamiento
    def _blob_path(self, exp_id: str, kind: str, name: str) -> Path:
        _check_id(kind, "tipo")
        _check_id(name, "archivo")
        return self.data_dir(exp_id) / kind / f"{name}.enc"

    def put(self, exp_id: str, kind: str, name: str, data: bytes) -> Path:
        """Cifra y guarda. kind: original | procesado | resultado | temporal ..."""
        path = self._blob_path(exp_id, kind, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.encrypt(exp_id, data, f"{kind}/{name}"))
        return path

    def get(self, exp_id: str, kind: str, name: str) -> bytes:
        path = self._blob_path(exp_id, kind, name)
        if not path.exists():
            raise FileNotFoundError(f"No existe {kind}/{name} en el expediente.")
        return self.decrypt(exp_id, path.read_bytes(), f"{kind}/{name}")

    def put_json(self, exp_id: str, kind: str, name: str, obj) -> Path:
        return self.put(exp_id, kind, name, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def get_json(self, exp_id: str, kind: str, name: str):
        return json.loads(self.get(exp_id, kind, name).decode("utf-8"))

    def list_names(self, exp_id: str, kind: str) -> list[str]:
        d = self.data_dir(exp_id) / _check_id(kind, "tipo")
        return sorted(p.name[:-4] for p in d.glob("*.enc")) if d.exists() else []


_service: Optional[EncryptionService] = None


def get_encryption_service() -> EncryptionService:
    global _service
    if _service is None:
        _service = EncryptionService()
    return _service
