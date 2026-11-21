"""Evaluacion de calidad y preprocesamiento de imagenes ANTES del OCR.

Metricas (todas calculadas sobre una version en gris normalizada a 1200 px de ancho,
para que sean comparables entre escaneos de distinta resolucion):
  - nitidez: varianza del Laplaciano
  - contraste texto/fondo: mediana (papel) menos percentil 0.3 (tinta). No se usa la desviacion
    estandar porque penaliza a las hojas con poco texto (p. ej. una hoja de firmas)
  - brillo medio (imagen demasiado oscura)
  - resolucion: lado menor de la imagen original
  - inclinacion (skew) y sospecha de rotacion de 90 grados (perfiles de proyeccion)
  - tinta pegada al borde (posible imagen incompleta / recortada)

Los umbrales vienen de Settings (configurables). NO son una prueba definitiva de
legibilidad: por eso hay dos niveles ("warning" y "critical") y la decision final
tambien considera la confianza del OCR (ver document_processor).

El archivo original nunca se modifica: todo ocurre sobre copias en memoria.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

import cv2
import numpy as np

from settings import Settings, get_settings

NORM_WIDTH = 1200


@dataclass
class QualityIssue:
    code: str
    severity: str          # "warning" | "critical"
    message: str           # lenguaje para el usuario


@dataclass
class QualityReport:
    metrics: dict = field(default_factory=dict)
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def critical(self) -> bool:
        return any(i.severity == "critical" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return bool(self.issues)

    def messages(self) -> list[str]:
        return [i.message for i in self.issues]

    def to_dict(self) -> dict:
        return {"metrics": self.metrics, "issues": [asdict(i) for i in self.issues]}


def to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        img = img[:, :, :3]
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _normalized(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape[:2]
    if w == NORM_WIDTH:
        return gray
    return cv2.resize(gray, (NORM_WIDTH, max(1, int(h * NORM_WIDTH / w))), interpolation=cv2.INTER_AREA)


def _binary_ink(gray_small: np.ndarray) -> np.ndarray:
    """Mascara de 'tinta' (texto) con umbral adaptativo; 1 = tinta."""
    blur = cv2.GaussianBlur(gray_small, (3, 3), 0)
    _, th = cv2.threshold(blur, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return th


def estimate_skew(gray: np.ndarray, max_angle: float = 8.0, step: float = 0.5) -> float:
    """Angulo de inclinacion en grados (positivo = antihorario) por perfil de proyeccion."""
    small = cv2.resize(gray, (600, max(1, int(gray.shape[0] * 600 / gray.shape[1]))), interpolation=cv2.INTER_AREA)
    ink = _binary_ink(small).astype(np.float32)
    if ink.sum() < 200:
        return 0.0
    h, w = ink.shape
    center = (w / 2, h / 2)
    best_angle, best_score = 0.0, -1.0
    for a in np.arange(-max_angle, max_angle + step, step):
        m = cv2.getRotationMatrix2D(center, float(a), 1.0)
        rot = cv2.warpAffine(ink, m, (w, h), flags=cv2.INTER_NEAREST)
        score = float(np.var(rot.sum(axis=1)))
        if score > best_score:
            best_angle, best_score = float(a), score
    return -best_angle if abs(best_angle) >= step else 0.0


def rotation_90_suspect(gray: np.ndarray) -> bool:
    """El texto en renglones horizontales produce un perfil de filas mas 'rugoso' que el de columnas.
    Si ocurre lo contrario, la pagina probablemente esta girada 90/270 grados."""
    small = cv2.resize(gray, (600, max(1, int(gray.shape[0] * 600 / gray.shape[1]))), interpolation=cv2.INTER_AREA)
    ink = _binary_ink(small).astype(np.float32)
    if ink.sum() < 200:
        return False
    rows = float(np.var(ink.sum(axis=1)) / max(ink.shape[1], 1))
    cols = float(np.var(ink.sum(axis=0)) / max(ink.shape[0], 1))
    return cols > rows * 1.5


def assess(img: np.ndarray, settings: Optional[Settings] = None) -> QualityReport:
    s = settings or get_settings()
    gray = to_gray(img)
    h, w = gray.shape[:2]
    norm = _normalized(gray)
    lap = float(cv2.Laplacian(norm, cv2.CV_64F).var())
    contrast = float(norm.std())
    ink_contrast = float(np.median(norm) - np.percentile(norm, 0.3))
    brightness = float(norm.mean())
    side = min(h, w)
    skew = estimate_skew(gray)
    rot90 = rotation_90_suspect(gray)

    ink = _binary_ink(cv2.resize(norm, (300, max(1, int(norm.shape[0] * 300 / norm.shape[1])))))
    band = max(2, int(0.015 * min(ink.shape)))
    edge = {"top": ink[:band].mean(), "bottom": ink[-band:].mean(), "left": ink[:, :band].mean(), "right": ink[:, -band:].mean()}
    total_ink = float(ink.mean())
    cut_sides = [k for k, v in edge.items() if v > max(0.12, 3.0 * total_ink)]

    rep = QualityReport(metrics={
        "width": int(w), "height": int(h), "sharpness_laplacian": round(lap, 1), "contrast_std": round(contrast, 1), "ink_contrast": round(ink_contrast, 1),
        "brightness_mean": round(brightness, 1), "skew_degrees": round(skew, 1), "rotation_90_suspect": bool(rot90),
        "ink_ratio": round(total_ink, 4), "edge_ink_sides": cut_sides,
    })

    def add(code, sev, msg):
        rep.issues.append(QualityIssue(code, sev, msg))

    # Pagina en blanco: no es "ilegible", simplemente no hay contenido (no se ejecuta OCR).
    if contrast < 3.5 and brightness > 200.0:
        add("pagina_en_blanco", "warning", "La página parece estar en blanco; no se extrajo texto.")
        rep.metrics["blank_page"] = True
        return rep
    rep.metrics["blank_page"] = False
    # Pagina con muy poco texto: la nitidez y el contraste globales no son confiables (sesgados por el
    # blanco), asi que no se descarta por metricas; decide la confianza del OCR.
    sparse = total_ink < 0.01
    rep.metrics["sparse_page"] = bool(sparse)
    crit = "warning" if sparse else "critical"
    if side < s.q_min_side_critical:
        add("baja_resolucion", "critical", f"Resolución muy baja ({w}x{h} px): el texto no se puede leer con fiabilidad.")
    elif side < s.q_min_side_warn:
        add("baja_resolucion", "warning", f"Resolución baja ({w}x{h} px): podrían perderse caracteres.")
    if lap < s.q_blur_critical:
        add("desenfoque", crit, "La imagen está muy borrosa y el texto no es legible.")
    elif lap < s.q_blur_warn:
        add("desenfoque", "warning", "La imagen está algo borrosa; algunos datos podrían leerse mal.")
    if brightness < s.q_dark_critical:
        add("muy_oscura", "critical", "La imagen está demasiado oscura.")
    elif brightness < s.q_dark_warn:
        add("muy_oscura", "warning", "La imagen está oscura; se mejoró el contraste, pero conviene volver a escanear.")
    if ink_contrast < s.q_contrast_critical:
        add("bajo_contraste", crit, "Contraste insuficiente: el texto casi no se distingue del fondo.")
    elif ink_contrast < s.q_contrast_warn:
        add("bajo_contraste", "warning", "Contraste bajo; se aplicó una mejora automática.")
    if abs(skew) >= 2.0:
        add("inclinada", "warning", f"La página está inclinada unos {abs(skew):.1f}°; se corrigió automáticamente.")
    if rot90:
        add("rotacion_incorrecta", "warning", "La página parece estar girada; se intentará corregir la orientación.")
    if len(cut_sides) >= 2:
        add("posible_recorte", "warning", "El contenido toca los bordes: la imagen podría estar incompleta o recortada.")
    return rep


def _rotate_keep_size(gray: np.ndarray, angle: float) -> np.ndarray:
    h, w = gray.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def preprocess_for_ocr(img: np.ndarray, report: Optional[QualityReport] = None, settings: Optional[Settings] = None) -> np.ndarray:
    """Copia procesada para OCR: gris, contraste (CLAHE), enderezado, nitidez moderada.
    Devuelve una imagen en gris (uint8). No toca el original."""
    s = settings or get_settings()
    gray = to_gray(img).copy()
    report = report or assess(gray, s)
    m = report.metrics
    if m.get("ink_contrast", 255) < s.q_contrast_warn or m.get("brightness_mean", 255) < s.q_dark_warn:
        gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    skew = m.get("skew_degrees", 0.0)
    if 0.5 <= abs(skew) <= 8.0:
        gray = _rotate_keep_size(gray, -skew)
    if m.get("sharpness_laplacian", 1e9) < s.q_blur_warn * 2:  # mejora moderada, solo si hace falta
        blur = cv2.GaussianBlur(gray, (0, 0), 1.2)
        gray = cv2.addWeighted(gray, 1.5, blur, -0.5, 0)
    return gray


def rotate90(img: np.ndarray, k: int) -> np.ndarray:
    """Gira k*90 grados en sentido horario (k en {1,2,3})."""
    return np.ascontiguousarray(np.rot90(img, -k))
