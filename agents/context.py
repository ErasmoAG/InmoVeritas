"""Seleccion de fragmentos relevantes: no se envia el documento completo al modelo."""
from __future__ import annotations

from agents.base import squash
from schemas.schemas import ProcessedDocument


def _chunks(doc: ProcessedDocument, size: int = 1400):
    for p in doc.pages:
        text = p.text.strip()
        if not text:
            continue
        lines, buf = text.split("\n"), ""
        for line in lines:
            if len(buf) + len(line) + 1 > size and buf:
                yield p.page, buf
                buf = ""
            buf += line + "\n"
        if buf.strip():
            yield p.page, buf


def select_context(doc: ProcessedDocument, keywords: list[str], max_chars: int = 9000) -> str:
    """Devuelve texto con marcas [Pagina N] (primer fragmento del documento + los mas relevantes por palabras clave)."""
    chunks = list(_chunks(doc))
    if not chunks:
        return ""
    kws = [squash(k) for k in keywords if squash(k)]
    scored = []
    for idx, (page, text) in enumerate(chunks):
        sq = squash(text)
        score = sum(sq.count(k) for k in kws)
        scored.append((score, idx, page, text))
    chosen: set[int] = {0}
    total = len(chunks[0][1])
    for score, idx, page, text in sorted(scored, key=lambda x: (-x[0], x[1])):
        if score <= 0 or idx in chosen:
            continue
        if total + len(text) > max_chars:
            continue
        chosen.add(idx)
        total += len(text)
    # si sobra espacio, completar con los siguientes fragmentos en orden
    for idx in range(len(chunks)):
        if total >= max_chars:
            break
        if idx not in chosen and total + len(chunks[idx][1]) <= max_chars:
            chosen.add(idx)
            total += len(chunks[idx][1])
    out, last_page = [], None
    for idx in sorted(chosen):
        page, text = chunks[idx]
        if page != last_page:
            out.append(f"[Página {page}]")
            last_page = page
        out.append(text.strip())
    return "\n".join(out)
