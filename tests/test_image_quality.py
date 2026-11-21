import io
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest
from PIL import Image

from services import image_quality as iq
from services.document_processor import process_document, process_image_page
from services.ocr_engines import OcrEngine, OcrOutput, get_engine
from settings import Settings

CONTEXTO = Path(__file__).resolve().parents[2] / "contexto"
S = Settings()


def page_image(lines=("PROPIETARIO: JUAN PEREZ LOPEZ", "SUPERFICIE: 450 M2", "CLAVE CATASTRAL 123456 EJERCICIO 2025",
                      "DIRECCION: CALLE REFORMA 15 COLONIA CENTRO"), size=(1700, 2200)):
    w, h = size
    img = np.full((h, w, 3), 255, np.uint8)
    y = 250
    for text in lines:
        cv2.putText(img, text, (120, y), cv2.FONT_HERSHEY_SIMPLEX, 1.9, (20, 20, 20), 3, cv2.LINE_AA)
        y += 160
    return img


def squash(t: str) -> str:
    return "".join(t.upper().split())


class CountingEngine(OcrEngine):
    name = "counting"

    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def ocr(self, img):
        self.calls += 1
        return self.inner.ocr(img)


class ExplodingEngine(OcrEngine):
    name = "exploding"

    def ocr(self, img):
        raise AssertionError("No debía ejecutarse OCR")


@pytest.fixture(scope="module")
def real_engine():
    return get_engine(S)


def pdf_from_images(images) -> bytes:
    doc = pymupdf.open()
    for img in images:
        ok, buf = cv2.imencode(".png", img)
        page = doc.new_page(width=612, height=792)
        page.insert_image(page.rect, stream=buf.tobytes())
    out = doc.tobytes()
    doc.close()
    return out


# ------------------------------------------------------------------ metricas
def test_imagen_nitida_sin_advertencias_graves():
    rep = iq.assess(page_image(), S)
    assert not rep.critical and rep.metrics["sharpness_laplacian"] > S.q_blur_warn


def test_desenfoque_critico_detectado():
    blurred = cv2.GaussianBlur(page_image(), (0, 0), 25)
    rep = iq.assess(blurred, S)
    assert rep.critical and any(i.code == "desenfoque" for i in rep.issues)


def test_imagen_oscura_detectada():
    dark = (page_image().astype(np.float32) * 0.12).astype(np.uint8)
    rep = iq.assess(dark, S)
    assert any(i.code == "muy_oscura" for i in rep.issues)


def test_baja_resolucion_detectada():
    small = cv2.resize(page_image(), (380, 490))
    rep = iq.assess(small, S)
    assert any(i.code == "baja_resolucion" and i.severity == "critical" for i in rep.issues)


def test_pagina_en_blanco_no_es_ilegible():
    rep = iq.assess(np.full((2200, 1700, 3), 255, np.uint8), S)
    assert rep.metrics["blank_page"] and not rep.critical


def test_inclinacion_estimada_y_corregida():
    img = page_image()
    h, w = img.shape[:2]
    tilted = cv2.warpAffine(img, cv2.getRotationMatrix2D((w / 2, h / 2), 4.0, 1.0), (w, h), borderValue=(255, 255, 255))
    skew = iq.estimate_skew(iq.to_gray(tilted))
    assert 3.0 <= abs(skew) <= 5.0


def test_original_no_se_modifica():
    img = page_image()
    before = img.copy()
    rep = iq.assess(img, S)
    iq.preprocess_for_ocr(img, rep, S)
    assert np.array_equal(img, before)


# ----------------------------------------------------------- procesamiento + OCR
def test_imagen_borrosa_no_ejecuta_ocr_ni_inventa_texto(real_engine):
    eng = CountingEngine(real_engine)
    page, _ = process_image_page(1, cv2.GaussianBlur(page_image(), (0, 0), 25), S, eng)
    assert page.status == "ilegible" and page.text == "" and page.source == "none"
    assert eng.calls == 0
    assert any("nueva imagen" in m.lower() or "nuevo" in m.lower() for m in page.issues)


def test_imagen_legible_se_lee_con_ocr(real_engine):
    page, png = process_image_page(1, page_image(), S, real_engine, keep_processed=True)
    assert page.status in ("ok", "baja_calidad") and page.source == "ocr"
    t = squash(page.text)
    assert "PROPIETARIO" in t and "2025" in t and page.ocr_confidence and page.ocr_confidence > 0.6
    assert png and png[:4] == b"\x89PNG"


@pytest.mark.parametrize("k", [1, 2, 3])
def test_pagina_girada_se_lee_correctamente(real_engine, k):
    """Giros de 90/180/270: el texto debe quedar legible, ya sea por el clasificador de angulo
    del motor OCR o por la correccion de orientacion del procesador."""
    rotated = np.ascontiguousarray(np.rot90(page_image(), k))
    page, _ = process_image_page(1, rotated, S, real_engine)
    assert "PROPIETARIO" in squash(page.text) and page.rotation_applied in (0, 90, 180, 270)


def test_pdf_con_texto_nativo_no_usa_ocr():
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "ESCRITURA PUBLICA NUMERO 123 PROPIETARIO JUAN PEREZ LOPEZ CON SUPERFICIE DE 450 METROS CUADRADOS", fontsize=9)
    data = doc.tobytes()
    out, _ = process_document("f1", "n.pdf", data, "pdf", S, engine=ExplodingEngine())
    assert out.pages[0].source == "native" and "JUAN PEREZ" in out.pages[0].text


def test_pdf_escaneado_usa_ocr_y_conserva_pagina(real_engine):
    data = pdf_from_images([page_image(), page_image(("SEGUNDA PAGINA SUPERFICIE 500 M2",))])
    out, _ = process_document("f1", "e.pdf", data, "pdf", S, engine=real_engine)
    assert [p.page for p in out.pages] == [1, 2] and all(p.source == "ocr" for p in out.pages)
    assert "SEGUNDAPAGINA" in squash(out.page_text(2)) and "PROPIETARIO" in squash(out.page_text(1))
    assert "[Página 2]" in out.full_text()


def test_limite_de_paginas_ocr_y_tiempo(real_engine):
    data = pdf_from_images([page_image()] * 3)
    out, _ = process_document("f", "x.pdf", data, "pdf", Settings(max_ocr_pages_per_doc=1), engine=real_engine)
    assert [p.status for p in out.pages][1:] == ["omitida", "omitida"] and out.pages[0].source == "ocr"
    out2, _ = process_document("f", "x.pdf", data, "pdf", S, engine=ExplodingEngine(), deadline=time.monotonic() - 1)
    assert all(p.status == "omitida" for p in out2.pages)


def test_imagen_png_y_tiff_multipagina(real_engine):
    ok, buf = cv2.imencode(".png", page_image())
    out, _ = process_document("f", "i.png", buf.tobytes(), "png", S, engine=real_engine)
    assert "PROPIETARIO" in squash(out.full_text())
    frames = [Image.fromarray(page_image()[:, :, ::-1]), Image.fromarray(page_image(("TERCERA HOJA",))[:, :, ::-1])]
    tiff = io.BytesIO()
    frames[0].save(tiff, "TIFF", save_all=True, append_images=frames[1:])
    out, _ = process_document("f", "m.tiff", tiff.getvalue(), "tiff", S, engine=real_engine)
    assert out.pages_total == 2 and "TERCERAHOJA" in squash(out.page_text(2))


# ----------------------------------------------------------- dataset real
def _member(zipname: str, member: str) -> bytes:
    with zipfile.ZipFile(CONTEXTO / zipname) as z:
        return z.read(member)


needs_data = pytest.mark.skipif(not (CONTEXTO / "Predial.zip").exists(), reason="Dataset no disponible")


@needs_data
def test_dataset_predial_legible_y_variante_degradada(real_engine):
    ok_doc, _ = process_document("a", "p.pdf", _member("Predial.zip", "Predial/02 Predial.pdf"), "pdf", S, engine=real_engine)
    t = squash(ok_doc.full_text())
    assert ok_doc.pages[0].status == "ok" and "PREDIAL" in t and "2025" in t
    bad, _ = process_document("b", "p2.pdf", _member("Predial.zip", "Predial/02 Predial...pdf"), "pdf", S, engine=ExplodingEngine())
    assert bad.pages[0].status == "ilegible" and not bad.has_text and bad.unreadable_pages == [1]


@needs_data
def test_dataset_texto_nativo_en_uso_de_suelo():
    doc, _ = process_document("a", "u.pdf", _member("Uso_de_suelo.zip", "Uso_de_suelo/USO DE SUELO-TULTEPEC INSURGENTES.pdf"),
                              "pdf", S, engine=ExplodingEngine())
    assert all(p.source == "native" for p in doc.pages) and len(doc.pages) == 3
