"""Utilidades compartidas por los agentes 7 a 10: parseo de fechas/numeros y acceso a campos extraidos."""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Optional

MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
          "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
_MONTH_RE = "|".join(MONTHS)


def _plain(t: str) -> str:
    return unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()


def parse_date(text: Optional[str]) -> Optional[date]:
    t = _plain(text or "")
    m = re.search(rf"(\d{{1,2}})\s*(?:de)?\s*({_MONTH_RE})\s*(?:de|del)?\s*(\d{{4}})", t)
    try:
        if m:
            return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
        m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", t)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = re.search(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", t)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return None


def parse_year(text: Optional[str]) -> Optional[int]:
    m = re.search(r"(19\d{2}|20\d{2})", text or "")
    return int(m.group(1)) if m else None


def parse_surface(text: Optional[str]) -> Optional[float]:
    t = (text or "").replace(" ", "")
    m = re.search(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)", t)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def get_field(extractions: list[dict], doc_type: str, name: str) -> list[tuple[dict, dict]]:
    """Devuelve [(extraccion, campo)] de los documentos de ese tipo donde el campo esta presente."""
    out = []
    for ex in extractions:
        if ex["doc_type"] == doc_type:
            f = ex["fields"].get(name)
            if f and f.get("status") == "present" and f.get("value"):
                out.append((ex, f))
    return out
