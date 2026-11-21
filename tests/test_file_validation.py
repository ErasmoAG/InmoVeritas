import io
import os
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from schemas.schemas import FileStatus
from services.file_validator import detect_format, validate_submission
from settings import Settings

CONTEXTO = Path(__file__).resolve().parents[2] / "contexto"


def noisy_image(fmt="PNG", size=(800, 600)) -> bytes:
    img = Image.effect_noise(size, 60).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def tiny_pdf(text="Hola InmoVeritas") -> bytes:
    import pymupdf as fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    out = doc.tobytes()
    doc.close()
    return out


def one(name, data, **kw):
    res = validate_submission([(name, data)], Settings(**kw) if kw else None)
    assert len(res) == 1
    return res[0]


# ------------------------------------------------------------- formatos validos
def test_pdf_valido():
    r = one("a.pdf", tiny_pdf())
    assert r.status == FileStatus.FORMATO_VALIDO and r.detected_format == "pdf" and r.pages == 1 and r.data


@pytest.mark.parametrize("fmt,ext,expected", [("PNG", "png", "png"), ("JPEG", "jpg", "jpeg"), ("JPEG", "jpeg", "jpeg"),
                                              ("WEBP", "webp", "webp"), ("TIFF", "tiff", "tiff"), ("TIFF", "tif", "tiff")])
def test_imagenes_validas(fmt, ext, expected):
    r = one(f"foto.{ext}", noisy_image(fmt))
    assert r.status == FileStatus.FORMATO_VALIDO, r.rejection
    assert r.detected_format == expected


def test_heic_valido():
    pillow_heif = pytest.importorskip("pillow_heif")
    img = Image.effect_noise((800, 600), 60).convert("RGB")
    buf = io.BytesIO()
    try:
        img.save(buf, "HEIF")
    except Exception as exc:  # el codificador puede no estar disponible
        pytest.skip(f"No se pudo generar HEIC de prueba: {exc}")
    r = one("foto.heic", buf.getvalue())
    assert r.status == FileStatus.FORMATO_VALIDO, r.rejection
    assert r.detected_format == "heif"


# ----------------------------------------------------------------- rechazos
def test_ejecutable_con_extension_pdf():
    r = one("factura.pdf", b"MZ\x90\x00" + b"\x00" * 1000)
    assert r.status == FileStatus.FORMATO_INVALIDO and r.rejection.code == "contenido_sospechoso"
    assert r.data is None


def test_extension_falsa_png_como_jpg():
    r = one("foto.jpg", noisy_image("PNG"))
    assert r.rejection.code == "extension_falsa"
    assert "PNG" in r.rejection.message


def test_extension_no_permitida():
    r = one("macro.docx", b"PK\x03\x04" + b"0" * 100)
    assert r.rejection.code == "extension_no_permitida"


def test_texto_con_extension_pdf():
    r = one("x.pdf", b"esto es solo texto" * 50)
    assert r.rejection.code == "firma_desconocida"


def test_pdf_corrupto():
    r = one("roto.pdf", b"%PDF-1.4\n" + b"basura" * 100)
    assert r.status == FileStatus.FORMATO_INVALIDO and r.rejection.code in {"pdf_corrupto", "pdf_ilegible", "pdf_vacio"}


def test_imagen_corrupta():
    data = noisy_image("PNG")
    r = one("rota.png", data[: len(data) // 2])
    assert r.rejection.code == "imagen_corrupta"


def test_imagen_muy_pequena_y_en_blanco():
    buf = io.BytesIO(); Image.new("RGB", (100, 100), "white").save(buf, "PNG")
    assert one("p.png", buf.getvalue()).rejection.code == "resolucion_muy_baja"
    buf = io.BytesIO(); Image.new("RGB", (800, 800), "white").save(buf, "PNG")
    assert one("b.png", buf.getvalue()).rejection.code == "imagen_en_blanco"


def test_archivo_vacio_y_muy_grande():
    assert one("v.pdf", b"").rejection.code == "archivo_vacio"
    r = one("g.pdf", tiny_pdf(), max_file_bytes=100)
    assert r.rejection.code == "archivo_muy_grande"


def test_mensaje_de_rechazo_explica_que_porque_y_como_corregir():
    r = one("x.exe", b"MZ" + b"\x00" * 600)
    assert r.rejection.message and r.rejection.hint


# --------------------------------------------------------------- duplicados
def test_duplicado_exacto_se_registra_y_no_se_reprocesa():
    pdf = tiny_pdf()
    res = validate_submission([("uno.pdf", pdf), ("copia.pdf", pdf)])
    assert res[0].status == FileStatus.FORMATO_VALIDO and res[0].data
    assert res[1].status == FileStatus.DUPLICADO and res[1].duplicate_of == res[0].file_id and res[1].data is None


def test_mismo_nombre_distinto_contenido_no_es_duplicado():
    res = validate_submission([("doc.pdf", tiny_pdf("uno")), ("doc.pdf", tiny_pdf("dos"))])
    assert [r.status for r in res] == [FileStatus.FORMATO_VALIDO, FileStatus.FORMATO_VALIDO]


def test_limite_de_archivos_por_envio():
    with pytest.raises(ValueError):
        validate_submission([("a.pdf", tiny_pdf())] * 3, Settings(max_files_per_submission=2))


# ---------------------------------------------------------------------- ZIP
def make_zip(entries: dict[str, bytes], compress=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress) as zf:
        for n, d in entries.items():
            zf.writestr(n, d)
    return buf.getvalue()


def test_zip_normal_expande_y_valida_miembros():
    z = make_zip({"carpeta/a.pdf": tiny_pdf("a"), "carpeta/b.png": noisy_image(), "__MACOSX/._a.pdf": b"x", "carpeta/.DS_Store": b"x"})
    res = validate_submission([("paquete.zip", z)])
    assert len(res) == 2 and all(r.status == FileStatus.FORMATO_VALIDO for r in res)
    assert all(r.source == "zip:paquete.zip" for r in res)
    assert any("omitieron" in w for r in res for w in r.warnings)


def test_zip_path_traversal_rechazado():
    z = make_zip({"../../evil.pdf": tiny_pdf(), "ok.pdf": tiny_pdf("ok")})
    res = validate_submission([("p.zip", z)])
    bad = [r for r in res if r.rejection and r.rejection.code == "ruta_peligrosa"]
    assert len(bad) == 1
    assert any(r.status == FileStatus.FORMATO_VALIDO for r in res)


def test_zip_bomba_por_tamano_descomprimido():
    z = make_zip({"grande.pdf": b"%PDF-1.4\n" + b"\x00" * (8 * 1024 * 1024)})
    res = validate_submission([("b.zip", z)], Settings(zip_max_uncompressed_bytes=2 * 1024 * 1024))
    assert res[0].status == FileStatus.FORMATO_INVALIDO and res[0].rejection.code == "zip_bomba"


def test_zip_demasiados_archivos():
    z = make_zip({f"{i}.pdf": tiny_pdf(str(i)) for i in range(5)})
    res = validate_submission([("m.zip", z)], Settings(zip_max_files=3))
    assert res[0].rejection.code == "zip_demasiados_archivos"


def test_zip_anidado_rechazado():
    inner = make_zip({"a.pdf": tiny_pdf()})
    res = validate_submission([("o.zip", make_zip({"i.zip": inner}))])
    assert res[0].rejection.code == "zip_anidado"


def test_zip_miembro_ejecutable_con_extension_pdf():
    res = validate_submission([("p.zip", make_zip({"a.pdf": b"MZ" + b"\x00" * 800}))])
    assert res[0].rejection.code == "contenido_sospechoso"


def test_zip_corrupto():
    res = validate_submission([("c.zip", b"PK\x03\x04" + b"\x00" * 200)])
    assert res[0].rejection.code == "zip_corrupto"


def test_duplicado_entre_zip_y_archivo_suelto():
    pdf = tiny_pdf()
    res = validate_submission([("suelto.pdf", pdf), ("p.zip", make_zip({"mismo.pdf": pdf}))])
    assert res[0].status == FileStatus.FORMATO_VALIDO
    assert res[1].status == FileStatus.DUPLICADO


def test_detect_format_basico():
    assert detect_format(b"%PDF-1.7") == "pdf" and detect_format(b"nada") is None


# --------------------------------------------------------- dataset real (si existe)
@pytest.mark.skipif(not (CONTEXTO / "Predial.zip").exists(), reason="Dataset no disponible")
@pytest.mark.parametrize("zipname,expected_real", [("Predial.zip", 17), ("Alineamiento_y_Numero_Oficial.zip", 18),
                                                   ("Uso_de_suelo.zip", 16), ("Escrituras.zip", 12)])
def test_dataset_real_zip_completo(zipname, expected_real, capsys):
    data = (CONTEXTO / zipname).read_bytes()
    res = validate_submission([(zipname, data)], Settings(max_file_bytes=80 * 1024 * 1024, max_pages_per_doc=400))
    ok = [r for r in res if r.status == FileStatus.FORMATO_VALIDO]
    dup = [r for r in res if r.status == FileStatus.DUPLICADO]
    bad = [r for r in res if r.status == FileStatus.FORMATO_INVALIDO]
    with capsys.disabled():
        print(f"\n{zipname}: validos={len(ok)} duplicados={len(dup)} rechazados={len(bad)}")
        for r in bad:
            print("   RECHAZADO:", r.original_name, "->", r.rejection.code)
        for r in dup:
            print("   DUPLICADO:", r.original_name)
    assert len(ok) + len(dup) + len(bad) == expected_real
    assert not any("__MACOSX" in r.original_name for r in res)
