"""Recepcion y validacion TECNICA de archivos (corre antes de cualquier agente/LLM).

Valida: extension permitida, firma binaria real (magic bytes), tamano, integridad,
legibilidad basica, duplicados exactos (SHA-256) y ZIP seguro.
NO evalua cumplimiento legal: eso corresponde a los agentes.

Los ZIP NO se extraen a disco: cada miembro se lee en memoria con tope de bytes,
asi no existe riesgo de escribir fuera del directorio del expediente.
"""
from __future__ import annotations

import hashlib
import io
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable, Optional

from settings import Settings, get_settings
from schemas.schemas import FileStatus, Rejection

# ------------------------------------------------------------------ catalogo
ALLOWED_EXTENSIONS = {
    "pdf": "pdf",
    "jpg": "jpeg", "jpeg": "jpeg",
    "png": "png",
    "webp": "webp",
    "tiff": "tiff", "tif": "tiff",
    "heic": "heif", "heif": "heif",
    "zip": "zip",
}
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1", b"heif"}
IGNORED_ZIP_MEMBERS = re.compile(r"(^|/)(__MACOSX/|\._[^/]*$|\.DS_Store$|Thumbs\.db$|desktop\.ini$)", re.I)

try:  # HEIC/HEIF
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pillow_heif = None


def _rej(code: str, message: str, hint: str) -> Rejection:
    return Rejection(code=code, message=message, hint=hint)


@dataclass
class ValidatedFile:
    file_id: str
    original_name: str
    source: str
    size_bytes: int
    sha256: str
    detected_format: Optional[str]
    status: FileStatus
    rejection: Optional[Rejection] = None
    duplicate_of: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    pages: int = 0
    data: Optional[bytes] = field(default=None, repr=False)  # solo si fue aceptado


# --------------------------------------------------------------- firma binaria
def detect_format(data: bytes) -> Optional[str]:
    """Devuelve el formato real segun los magic bytes, o None si no es reconocido."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS:
        return "heif"
    if data.startswith(b"PK\x03\x04"):
        return "zip"
    return None


def is_executable_like(data: bytes) -> Optional[str]:
    head = data[:512]
    if head.startswith(b"MZ"):
        return "ejecutable de Windows"
    if head.startswith(b"\x7fELF"):
        return "ejecutable de Linux"
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        return "ejecutable de macOS"
    if head.startswith(b"#!"):
        return "script ejecutable"
    low = head.lstrip().lower()
    if low.startswith((b"<script", b"<?php", b"<html", b"<!doctype html")):
        return "contenido web/script"
    return None


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def safe_name(name: str) -> str:
    """Nombre seguro para mostrar/guardar (sin rutas ni caracteres peligrosos)."""
    base = PurePosixPath(name.replace("\\", "/")).name
    base = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", base).strip(" .")
    return base[:150] or "archivo"


# ------------------------------------------------------------ integridad/legibilidad
def _check_pdf(data: bytes, s: Settings) -> tuple[Optional[Rejection], int, list[str]]:
    import pymupdf as fitz  # PyMuPDF
    warnings: list[str] = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        return _rej("pdf_corrupto", "El PDF está dañado y no se puede abrir.",
                    "Vuelva a generar o descargar el PDF y súbalo de nuevo."), 0, warnings
    try:
        if doc.needs_pass or doc.is_encrypted:
            return _rej("pdf_protegido", "El PDF está protegido con contraseña.",
                        "Suba una versión sin contraseña."), 0, warnings
        n = doc.page_count
        if n < 1:
            return _rej("pdf_vacio", "El PDF no contiene páginas.", "Suba un PDF con contenido."), 0, warnings
        if n > s.max_pages_per_doc:
            return _rej("demasiadas_paginas", f"El PDF tiene {n} páginas y el máximo permitido es {s.max_pages_per_doc}.",
                        "Divida el documento o suba solo las páginas relevantes."), n, warnings
        try:  # legibilidad basica: la primera pagina debe poder renderizarse
            doc[0].get_pixmap(matrix=fitz.Matrix(0.15, 0.15))
        except Exception:
            return _rej("pdf_ilegible", "No se pudo leer la primera página del PDF.",
                        "El archivo podría estar dañado; súbalo de nuevo."), n, warnings
        return None, n, warnings
    finally:
        doc.close()


def _check_image(data: bytes, fmt: str, s: Settings) -> tuple[Optional[Rejection], int, list[str]]:
    from PIL import Image, ImageStat
    warnings: list[str] = []
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            w, h = im.size
            frames = getattr(im, "n_frames", 1)
            if frames > s.max_pages_per_doc:
                return _rej("demasiadas_paginas", f"La imagen tiene {frames} páginas (máximo {s.max_pages_per_doc}).",
                            "Divida el archivo."), frames, warnings
            gray = im.convert("L").resize((min(w, 256), min(h, 256)))
            std = ImageStat.Stat(gray).stddev[0]
    except Exception:
        return _rej("imagen_corrupta", "La imagen está dañada o incompleta y no se puede abrir.",
                    "Tome o exporte de nuevo la imagen y súbala otra vez."), 0, warnings
    if min(w, h) < s.min_image_side_px:
        return _rej("resolucion_muy_baja", f"La imagen es demasiado pequeña ({w}x{h} px) para leerse.",
                    f"Tome la foto o escaneo con al menos {s.min_image_side_px} px de lado; idealmente más de 1500 px."), frames, warnings
    if std < 3.0:
        return _rej("imagen_en_blanco", "La imagen parece vacía o de un solo color.",
                    "Verifique que el documento aparezca en la foto y súbala de nuevo."), frames, warnings
    return None, frames, warnings


# --------------------------------------------------------------------- nucleo
def _validate_one(name: str, data: bytes, source: str, s: Settings) -> ValidatedFile:
    fid = uuid.uuid4().hex[:12]
    sha = hashlib.sha256(data).hexdigest()
    shown = safe_name(name)
    base = dict(file_id=fid, original_name=shown, source=source, size_bytes=len(data), sha256=sha)

    def reject(rej: Rejection, fmt: Optional[str] = None) -> ValidatedFile:
        return ValidatedFile(**base, detected_format=fmt, status=FileStatus.FORMATO_INVALIDO, rejection=rej)

    if len(data) == 0:
        return reject(_rej("archivo_vacio", f"«{shown}» está vacío.", "Suba el archivo correcto."))
    if len(data) > s.max_file_bytes:
        mb = s.max_file_bytes // (1024 * 1024)
        return reject(_rej("archivo_muy_grande", f"«{shown}» pesa {len(data)/1048576:.1f} MB y el máximo es {mb} MB.",
                           "Comprima el PDF o reduzca la resolución de la imagen."))
    exe = is_executable_like(data)
    if exe:
        return reject(_rej("contenido_sospechoso", f"«{shown}» contiene un {exe}, aunque su nombre sugiera otro tipo.",
                           "Solo se aceptan documentos PDF, imágenes o ZIP con ellos."))
    ext = _ext(shown)
    if ext not in ALLOWED_EXTENSIONS:
        return reject(_rej("extension_no_permitida", f"«{shown}» tiene una extensión no permitida (.{ext or 'sin extensión'}).",
                           "Formatos admitidos: PDF, JPG, JPEG, PNG, WEBP, TIFF, TIF, HEIC, HEIF y ZIP."))
    real = detect_format(data)
    if real is None:
        return reject(_rej("firma_desconocida", f"El contenido de «{shown}» no corresponde a un formato permitido.",
                           "Verifique que el archivo no esté dañado y que sea PDF, imagen o ZIP."))
    if real != ALLOWED_EXTENSIONS[ext]:
        return reject(_rej("extension_falsa", f"«{shown}» dice ser .{ext} pero su contenido real es {real.upper()}.",
                           "Suba el archivo con su formato original, sin renombrar la extensión."), real)
    if real == "zip":
        return reject(_rej("zip_anidado", f"«{shown}» es un ZIP dentro de otro ZIP, no permitido.",
                           "Suba los documentos directamente en un solo ZIP."), real)

    if real == "pdf":
        rej, pages, warns = _check_pdf(data, s)
    else:
        rej, pages, warns = _check_image(data, real, s)
    if rej:
        return reject(rej, real)
    return ValidatedFile(**base, detected_format=real, status=FileStatus.FORMATO_VALIDO, pages=pages, warnings=warns, data=data)


def _zip_members(name: str, data: bytes, s: Settings) -> tuple[list[tuple[str, bytes]], list[ValidatedFile], list[str]]:
    """Inspecciona un ZIP. Devuelve (miembros_validos_en_memoria, rechazos_de_miembros, avisos).
    Si el ZIP completo es inaceptable lanza ValueError con un Rejection en args[0]."""
    shown = safe_name(name)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError(_rej("zip_corrupto", f"«{shown}» está dañado y no se puede abrir.", "Vuelva a comprimir los documentos y súbalo de nuevo."))
    infos = [i for i in zf.infolist() if not i.is_dir()]
    notes: list[str] = []
    ignored = [i for i in infos if IGNORED_ZIP_MEMBERS.search(i.filename.replace("\\", "/"))]
    infos = [i for i in infos if i not in ignored]
    if ignored:
        notes.append(f"Se omitieron {len(ignored)} archivos de sistema (por ejemplo __MACOSX) dentro de «{shown}».")
    if len(infos) == 0:
        raise ValueError(_rej("zip_vacio", f"«{shown}» no contiene documentos.", "Agregue los documentos al ZIP."))
    if len(infos) > s.zip_max_files:
        raise ValueError(_rej("zip_demasiados_archivos", f"«{shown}» contiene {len(infos)} archivos y el máximo es {s.zip_max_files}.", "Divida el ZIP en partes."))
    declared = sum(i.file_size for i in infos)
    if declared > s.zip_max_uncompressed_bytes:
        raise ValueError(_rej("zip_bomba", f"«{shown}» se descomprimiría a {declared/1048576:.0f} MB, más del límite permitido.", "Suba menos documentos o con menor tamaño."))
    if len(data) > 0 and declared / max(len(data), 1) > s.zip_max_ratio and declared > 50 * 1048576:
        raise ValueError(_rej("zip_bomba", f"«{shown}» tiene una tasa de compresión sospechosa.", "Suba los documentos sin comprimir en exceso."))

    good: list[tuple[str, bytes]] = []
    rejected: list[ValidatedFile] = []
    total = 0
    for info in infos:
        member = info.filename.replace("\\", "/")
        label = f"{shown} › {safe_name(member)}"
        problem: Optional[Rejection] = None
        parts = PurePosixPath(member).parts
        if member.startswith("/") or re.match(r"^[A-Za-z]:", member) or ".." in parts:
            problem = _rej("ruta_peligrosa", f"«{label}» usa una ruta peligrosa dentro del ZIP.", "Vuelva a crear el ZIP sin rutas absolutas ni «..».")
        elif info.flag_bits & 0x1:
            problem = _rej("zip_cifrado", f"«{label}» está cifrado con contraseña.", "Suba una versión sin contraseña.")
        elif (info.external_attr >> 16) & 0o170000 == 0o120000:
            problem = _rej("zip_enlace_simbolico", f"«{label}» es un enlace simbólico, no permitido.", "Incluya el archivo real.")
        elif info.file_size > s.max_file_bytes:
            problem = _rej("archivo_muy_grande", f"«{label}» excede el tamaño máximo.", "Reduzca su tamaño.")
        content = b""
        if problem is None:
            # Lectura con tope real (no se confia en el tamano declarado).
            try:
                with zf.open(info) as fh:
                    content = fh.read(min(s.max_file_bytes, s.zip_max_uncompressed_bytes - total) + 1)
            except Exception:
                problem = _rej("zip_miembro_corrupto", f"«{label}» está dañado dentro del ZIP.", "Vuelva a comprimirlo.")
            else:
                total += len(content)
                if len(content) > s.max_file_bytes or total > s.zip_max_uncompressed_bytes:
                    raise ValueError(_rej("zip_bomba", f"«{shown}» excede el tamaño descomprimido permitido.", "Suba menos documentos."))
        if problem:
            rejected.append(ValidatedFile(file_id=uuid.uuid4().hex[:12], original_name=safe_name(member), source=f"zip:{shown}",
                                          size_bytes=info.file_size, sha256="", detected_format=None,
                                          status=FileStatus.FORMATO_INVALIDO, rejection=problem))
        else:
            good.append((member, content))
    return good, rejected, notes




def _validate_zip_container(shown: str, data: bytes, s: Settings) -> Optional[ValidatedFile]:
    """Chequeos previos del propio ZIP (tamano, ejecutable, extension falsa)."""
    sha = hashlib.sha256(data).hexdigest()

    def bad(rej: Rejection) -> ValidatedFile:
        return ValidatedFile(file_id=uuid.uuid4().hex[:12], original_name=shown, source="upload", size_bytes=len(data),
                             sha256=sha, detected_format=detect_format(data), status=FileStatus.FORMATO_INVALIDO, rejection=rej)

    if len(data) == 0:
        return bad(_rej("archivo_vacio", f"«{shown}» está vacío.", "Suba el archivo correcto."))
    if len(data) > s.max_file_bytes:
        return bad(_rej("archivo_muy_grande", f"«{shown}» pesa {len(data)/1048576:.1f} MB y el máximo es {s.max_file_bytes//1048576} MB.", "Divida el ZIP."))
    exe = is_executable_like(data)
    if exe:
        return bad(_rej("contenido_sospechoso", f"«{shown}» contiene un {exe}.", "Solo se aceptan documentos y sus ZIP."))
    real = detect_format(data)
    if real != "zip":
        return bad(_rej("extension_falsa", f"«{shown}» dice ser ZIP pero su contenido real es {real.upper() if real else 'desconocido'}.",
                        "Suba el archivo con su formato original."))
    if _ext(shown) != "zip":
        return bad(_rej("extension_falsa", f"«{shown}» es un ZIP pero tiene otra extensión.", "Use la extensión .zip."))
    return None


def validate_submission(items: Iterable[tuple[str, bytes]], settings: Optional[Settings] = None) -> list[ValidatedFile]:
    """Valida todos los archivos de un envio (expandiendo ZIP) y marca duplicados exactos.

    `items`: pares (nombre_original, bytes). Devuelve un resultado por archivo,
    incluyendo los miembros de los ZIP. Los duplicados se detectan por SHA-256 sobre
    el contenido (no por nombre), asi que dos archivos distintos con el mismo nombre
    NO se confunden.
    """
    s = settings or get_settings()
    items = list(items)
    if len(items) > s.max_files_per_submission:
        raise ValueError(f"Se enviaron {len(items)} archivos y el máximo es {s.max_files_per_submission}.")

    results: list[ValidatedFile] = []
    flat: list[tuple[str, bytes, str, list[str]]] = []  # (nombre, datos, origen, avisos)

    for name, data in items:
        shown = safe_name(name)
        if _ext(shown) == "zip":
            container_problem = _validate_zip_container(shown, data, s)
            if container_problem is not None:
                results.append(container_problem)
                continue
            try:
                good, rejected, notes = _zip_members(shown, data, s)
            except ValueError as exc:
                results.append(ValidatedFile(file_id=uuid.uuid4().hex[:12], original_name=shown, source="upload",
                                             size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                                             detected_format="zip", status=FileStatus.FORMATO_INVALIDO,
                                             rejection=exc.args[0]))
                continue
            results.extend(rejected)
            for member, content in good:
                flat.append((member, content, f"zip:{shown}", list(notes)))
        else:
            flat.append((name, data, "upload", []))

    seen: dict[str, str] = {}  # sha256 -> file_id del primero aceptado
    for name, data, source, notes in flat:
        vf = _validate_one(name, data, source, s)
        vf.warnings.extend(notes)
        if vf.status == FileStatus.FORMATO_VALIDO:
            if vf.sha256 in seen:
                vf.status = FileStatus.DUPLICADO
                vf.duplicate_of = seen[vf.sha256]
                vf.data = None
                vf.rejection = _rej("duplicado", f"«{vf.original_name}» es idéntico a otro archivo ya recibido.",
                                    "No necesita volver a subirlo; se analizará una sola copia.")
            else:
                seen[vf.sha256] = vf.file_id
        results.append(vf)
    return results
