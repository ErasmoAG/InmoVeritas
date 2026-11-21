"""Genera .env a partir de .env.example con claves aleatorias nuevas.

Uso:  python scripts/setup_env.py
No sobrescribe un .env existente (para no perder la clave maestra: sin ella los
expedientes cifrados quedan irrecuperables). Las claves no se imprimen.
"""
import base64
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
example, target = ROOT / ".env.example", ROOT / ".env"

if target.exists():
    sys.exit(".env ya existe; no se modifica. Bórrelo manualmente solo si acepta perder acceso a los expedientes cifrados.")

values = {
    "INMO_MASTER_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
    "INMO_SESSION_SECRET": secrets.token_urlsafe(48),
}
out = []
for line in example.read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0].strip()
    out.append(f"{key}={values[key]}" if key in values and line.rstrip().endswith("=") else line)
target.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f".env creado en {target} con claves nuevas (no se muestran). Respáldelo en un lugar seguro.")
