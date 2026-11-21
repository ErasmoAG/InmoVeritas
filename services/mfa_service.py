"""Autenticacion del panel del abogado: contrasena (bcrypt) + segundo factor TOTP (pyotp).

- El secreto TOTP se guarda CIFRADO (AES-GCM con la clave maestra), vinculado a la cuenta.
- El codigo TOTP nunca se usa como clave de cifrado.
- Bloqueo basico por intentos fallidos (en memoria; limitacion del MVP)."""
from __future__ import annotations

import base64
import io
import json
import os
import time
from typing import Optional

import bcrypt
import pyotp
import qrcode
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.encryption_service import _load_master
from settings import get_settings

ISSUER = "Cotejo"
MAX_FAILS, LOCK_SECONDS = 5, 600
_fails: dict[str, list[float]] = {}


def _users_path():
    return get_settings().storage_dir / "users.json"


def _load() -> dict:
    p = _users_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _wrap(secret: str, username: str) -> dict:
    nonce = os.urandom(12)
    ct = AESGCM(_load_master()).encrypt(nonce, secret.encode(), f"totp:{username}".encode())
    return {"nonce": base64.b64encode(nonce).decode(), "ct": base64.b64encode(ct).decode()}


def _unwrap(blob: dict, username: str) -> str:
    return AESGCM(_load_master()).decrypt(base64.b64decode(blob["nonce"]), base64.b64decode(blob["ct"]), f"totp:{username}".encode()).decode()


def create_user(username: str, password: str) -> tuple[str, str]:
    """Crea (o reemplaza) la cuenta. Devuelve (secreto_base32, uri_de_aprovisionamiento) para escanear una sola vez."""
    if len(password) < 10:
        raise ValueError("La contraseña debe tener al menos 10 caracteres.")
    users = _load()
    secret = pyotp.random_base32()
    users[username] = {"pw": bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(), "totp": _wrap(secret, username)}
    p = _users_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(users), encoding="utf-8")
    return secret, pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def has_users() -> bool:
    return bool(_load())


def _locked(username: str) -> bool:
    now = time.time()
    _fails[username] = [t for t in _fails.get(username, []) if now - t < LOCK_SECONDS]
    return len(_fails[username]) >= MAX_FAILS


def verify_password(username: str, password: str) -> bool:
    if _locked(username):
        return False
    u = _load().get(username)
    ok = bool(u) and bcrypt.checkpw(password.encode(), u["pw"].encode())
    if not ok:
        _fails.setdefault(username, []).append(time.time())
    return ok


def verify_totp(username: str, code: str) -> bool:
    if _locked(username):
        return False
    u = _load().get(username)
    if not u:
        return False
    ok = pyotp.TOTP(_unwrap(u["totp"], username)).verify((code or "").strip().replace(" ", ""), valid_window=1)
    if not ok:
        _fails.setdefault(username, []).append(time.time())
    return ok


def is_locked(username: str) -> bool:
    return _locked(username)


def qr_png_b64(uri: str) -> str:
    buf = io.BytesIO()
    qrcode.make(uri).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()
