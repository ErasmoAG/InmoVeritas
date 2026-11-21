"""Extraccion de texto por pagina: texto nativo (PyMuPDF) y OCR local solo cuando hace falta.

Para cada pagina conserva: numero de pagina, texto, origen (native/ocr), confianza del OCR,
estado de legibilidad y las advertencias de calidad. Nunca inventa texto: una pagina ilegible
queda vacia y marcada para revision o sustitucion.

Orden de decisiones por pagina escaneada:
  1) calidad de imagen (image_quality.assess) -> en blanco / critica => NO se hace OCR
  2) preprocesamiento sobre una COPIA (gris, contraste, enderezado, nitidez moderada)
  3) OCR local; si la confianza es baja o se sospecha rotacion, prueba 90/180/270 grados
  4) legibilidad final por confianza del OCR y cantidad de texto
"""
from __future__ import annotations

import io
import time
from typing import Callable, Optional

import cv2
import numpy as np
import pymupdf
from PIL import Image, ImageOps

from schemas.schemas import PageContent, ProcessedDocument
from services import image_quality as iq
from services.ocr_engines import OcrEngine, OcrOutput, get_engine
from settings import Settings, get_settings

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

DEGRADING = {"desenfoque", "baja_resolucion", "muy_oscura", "bajo_contraste", "posible_recorte"}
MIN_CHARS_LEGIBLE = 15


def _cap_side(img: np.ndarray, max_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    if max(h, w) <= max_side:
        return img
    f = max_side / max(h, w)
    return cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)


def _score(out: OcrOutput) -> float:
    conf = out.mean_conf if out.mean_conf is not None else max(0.0, 1.0 - out.garbage_ratio * 3)
    return out.chars * conf


def _confidence_ok(out: OcrOutput, s: Settings) -> bool:
    if out.mean_conf is not None:  # una hoja corta (p. ej. de firmas) puede ser legible con pocos caracteres
        return out.chars >= 3 and out.mean_conf >= s.ocr_min_confidence
    return out.chars >= MIN_CHARS_LEGIBLE and out.garbage_ratio < 0.08  # motores sin confianza


def process_image_page(page_no: int, img: np.ndarray, settings: Settings, engine: OcrEngine,
                       keep_processed: bool = False) -> tuple[PageContent, Optional[bytes]]:
    """Analiza una imagen de pagina (BGR/gris). Devuelve el contenido y, opcionalmente, la copia procesada en PNG."""
    s = settings
    img = _cap_side(img, s.ocr_max_side_px)
    rep = iq.assess(img, s)
    page = PageContent(page=page_no, quality=rep.to_dict()["metrics"])
    page.issues = rep.messages()

    if rep.metrics.get("blank_page"):
        page.status, page.source = "en_blanco", "none"
        return page, None
    if rep.critical:
        page.status, page.source = "ilegible", "none"
        page.issues.append("Se omitió la extracción de datos para no generar información con confianza artificial. "
                           "Solicite una nueva imagen o escaneo de esta página.")
        return page, None

    pre = iq.preprocess_for_ocr(img, rep, s)
    out = engine.ocr(pre)
    rotation = 0
    suspect = bool(rep.metrics.get("rotation_90_suspect"))
    conf = out.mean_conf if out.mean_conf is not None else 1.0
    # Solo se reintenta con otras orientaciones si el resultado es dudoso (cada intento cuesta un OCR completo).
    if not _confidence_ok(out, s) or (suspect and conf < 0.8):
        best, best_k = out, 0
        for k in ((1, 3) if suspect else (2, 1, 3)):  # 90/270 si se sospecha giro lateral; si no, 180 primero
            cand = engine.ocr(iq.rotate90(pre, k))
            if _score(cand) > _score(best) * 1.15:
                best, best_k = cand, k
                if _confidence_ok(best, s) and (best.mean_conf or 1.0) >= 0.8:
                    break
        if best_k:
            out, rotation = best, best_k * 90
            page.issues.append(f"Se corrigió la orientación de la página ({rotation}°).")
    page.rotation_applied = rotation
    page.source = "ocr"
    page.text = out.text
    page.ocr_confidence = round(out.mean_conf, 3) if out.mean_conf is not None else None

    if not _confidence_ok(out, s):
        page.status = "ilegible"
        page.text = ""  # no se conserva texto dudoso como si fuera confiable
        page.source = "none"
        page.issues.append("El OCR no logró leer esta página con suficiente confianza. Solicite una nueva imagen o escaneo.")
    elif any(i.code in DEGRADING for i in rep.issues):
        page.status = "baja_calidad"
    else:
        page.status = "ok"

    png = None
    if keep_processed:
        ok, buf = cv2.imencode(".png", pre)
        png = buf.tobytes() if ok else None
    return page, png


def _pdf_page_image(page: "pymupdf.Page", dpi: int) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return arr[:, :, ::-1].copy() if pix.n == 3 else arr[:, :, 0].copy()


def process_document(file_id: str, filename: str, data: bytes, fmt: str, settings: Optional[Settings] = None,
                     engine: Optional[OcrEngine] = None, keep_processed: bool = False,
                     deadline: Optional[float] = None,
                     on_page: Optional[Callable[[int, int], None]] = None) -> tuple[ProcessedDocument, dict[int, bytes]]:
    """Procesa un PDF o imagen ya validados. `deadline` es un instante de time.monotonic()."""
    s = settings or get_settings()
    t0 = time.monotonic()
    doc_out = ProcessedDocument(file_id=file_id, filename=filename)
    processed: dict[int, bytes] = {}
    eng: Optional[OcrEngine] = engine
    ocr_pages = 0

    def engine_lazy() -> OcrEngine:
        nonlocal eng
        if eng is None:
            eng = get_engine(s)
        return eng

    def skipped(no: int, why: str) -> PageContent:
        return PageContent(page=no, status="omitida", source="none", issues=[why])

    if fmt == "pdf":
        pdf = pymupdf.open(stream=data, filetype="pdf")
        try:
            doc_out.pages_total = pdf.page_count
            for i in range(pdf.page_count):
                no = i + 1
                if on_page:
                    on_page(no, pdf.page_count)
                page = pdf[i]
                native = page.get_text("text").strip()
                if len(native) >= s.native_text_min_chars:
                    doc_out.pages.append(PageContent(page=no, text=native, source="native", status="ok"))
                    continue
                if ocr_pages >= s.max_ocr_pages_per_doc:
                    doc_out.pages.append(skipped(no, f"No se analizó: se alcanzó el límite de {s.max_ocr_pages_per_doc} páginas con OCR por documento."))
                    continue
                if deadline is not None and time.monotonic() > deadline:
                    doc_out.pages.append(skipped(no, "No se analizó: se agotó el tiempo máximo de procesamiento."))
                    continue
                ocr_pages += 1
                pc, png = process_image_page(no, _pdf_page_image(page, s.ocr_dpi), s, engine_lazy(), keep_processed)
                doc_out.pages.append(pc)
                if png:
                    processed[no] = png
        finally:
            pdf.close()
    else:  # imagenes (JPG, PNG, WEBP, TIFF, HEIC...)
        with Image.open(io.BytesIO(data)) as im:
            frames = getattr(im, "n_frames", 1)
            doc_out.pages_total = frames
            for i in range(frames):
                no = i + 1
                if on_page:
                    on_page(no, frames)
                if ocr_pages >= s.max_ocr_pages_per_doc:
                    doc_out.pages.append(skipped(no, f"No se analizó: se alcanzó el límite de {s.max_ocr_pages_per_doc} páginas."))
                    continue
                if deadline is not None and time.monotonic() > deadline:
                    doc_out.pages.append(skipped(no, "No se analizó: se agotó el tiempo máximo de procesamiento."))
                    continue
                im.seek(i)
                frame = ImageOps.exif_transpose(im.copy()).convert("RGB")  # respeta la orientacion del celular
                arr = np.asarray(frame)[:, :, ::-1].copy()
                ocr_pages += 1
                pc, png = process_image_page(no, arr, s, engine_lazy(), keep_processed)
                doc_out.pages.append(pc)
                if png:
                    processed[no] = png

    doc_out.ocr_engine = eng.name if eng else None
    doc_out.elapsed_ms = int((time.monotonic() - t0) * 1000)
    return doc_out, processed
