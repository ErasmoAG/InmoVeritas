"""Crea la cuenta del abogado (contrasena + TOTP). Uso: python scripts/create_user.py
La contrasena se pide por consola (no se muestra ni se guarda en texto). El QR se guarda en
storage/ALTA_TOTP_<usuario>.png: escanéelo con su app (Google Authenticator, Authy...) y BÓRRELO."""
import base64
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import settings  # noqa: E402,F401
from services import mfa_service as auth  # noqa: E402

user = input("Usuario: ").strip()
pw = getpass.getpass("Contraseña (mínimo 10 caracteres): ")
if pw != getpass.getpass("Repita la contraseña: "):
    sys.exit("Las contraseñas no coinciden.")
secret, uri = auth.create_user(user, pw)
out = ROOT / "storage" / f"ALTA_TOTP_{user}.png"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(base64.b64decode(auth.qr_png_b64(uri)))
print(f"\nCuenta creada. Escanee el código QR: {out}\n(Si no puede escanear, use esta clave manual en su app: {secret})\nBorre el archivo PNG después de registrarlo.")
