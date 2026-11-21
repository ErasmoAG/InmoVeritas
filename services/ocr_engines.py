"""Motores OCR LOCALES (ningun documento sale del equipo).

- rapidocr : RapidOCR / ONNX en CPU. Preciso con digitos (~2 s por pagina medido en este equipo).
             No separa bien las palabras en espanol ("DESANJERONIMO").
- windows  : OCR integrado de Windows 10/11 (Windows.Media.Ocr). ~0.2 s por pagina, separa
             palabras y acentos, pero NO entrega confianza y confunde algunos digitos
             (medido: "TM-2025-435320" -> "TM-2ô25-43532ô"). Solo disponible en Windows.

El motor se elige con OCR_ENGINE. Si el pedido no esta disponible se usa rapidocr y se
informa cual se uso realmente (OcrEngine.name).
"""
from __future__ import annotations

import asyncio
import statistics
import threading
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from settings import Settings, get_settings


@dataclass
class OcrLine:
    text: str
    conf: Optional[float]
    box: tuple  # (x0, y0, x1, y1)


@dataclass
class OcrOutput:
    lines: list[OcrLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        return layout_text(self.lines)

    @property
    def chars(self) -> int:
        return sum(len(l.text.strip()) for l in self.lines)

    @property
    def mean_conf(self) -> Optional[float]:
        confs = [l.conf for l in self.lines if l.conf is not None]
        if not confs:
            return None
        # ponderada por longitud: una linea larga pesa mas que un ruido de 2 letras
        w = [max(len(l.text), 1) for l in self.lines if l.conf is not None]
        return float(sum(c * x for c, x in zip(confs, w)) / sum(w))

    @property
    def garbage_ratio(self) -> float:
        """Proporcion de caracteres raros (sin letras/digitos/puntuacion comun): util sin confianza."""
        t = "".join(l.text for l in self.lines)
        if not t:
            return 1.0
        ok = sum(ch.isalnum() or ch.isspace() or ch in ".,;:-/()#%$°ºª'\"&@+_" for ch in t)
        return 1.0 - ok / len(t)


def layout_text(lines: list[OcrLine]) -> str:
    """Ordena las lineas de arriba abajo e izquierda a derecha y agrupa las de un mismo renglon."""
    if not lines:
        return ""
    heights = [max(1, l.box[3] - l.box[1]) for l in lines]
    tol = max(4.0, 0.6 * statistics.median(heights))
    ordered = sorted(lines, key=lambda l: ((l.box[1] + l.box[3]) / 2, l.box[0]))
    rows: list[list[OcrLine]] = []
    for l in ordered:
        cy = (l.box[1] + l.box[3]) / 2
        if rows and abs(cy - statistics.mean((r.box[1] + r.box[3]) / 2 for r in rows[-1])) <= tol:
            rows[-1].append(l)
        else:
            rows.append([l])
    return "\n".join(" ".join(x.text.strip() for x in sorted(r, key=lambda l: l.box[0]) if x.text.strip()) for r in rows)


class OcrEngine:
    name = "base"

    def ocr(self, img: np.ndarray) -> OcrOutput:  # img: gris o BGR uint8
        raise NotImplementedError


class RapidOcrEngine(OcrEngine):
    name = "rapidocr"

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self._lock = threading.Lock()
        self.accel = "cpu"
        engine = None
        try:  # aceleracion por GPU via DirectML (Windows); mismo resultado, mucho mas rapido
            import onnxruntime as ort
            if "DmlExecutionProvider" in ort.get_available_providers():
                engine = RapidOCR(det_use_dml=True, cls_use_dml=True, rec_use_dml=True)
                engine(np.full((64, 256, 3), 255, np.uint8))  # prueba rapida de que funciona
                self.accel = "directml"
        except Exception:
            engine = None
        if engine is None:
            engine = RapidOCR(intra_op_num_threads=4, inter_op_num_threads=1)
            self.accel = "cpu"
        self._engine = engine
        self.name = f"rapidocr-{self.accel}"

    def ocr(self, img: np.ndarray) -> OcrOutput:
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        with self._lock:
            res, _ = self._engine(img)
        out = OcrOutput()
        for box, text, score in (res or []):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.lines.append(OcrLine(text=str(text), conf=float(score), box=(min(xs), min(ys), max(xs), max(ys))))
        return out


class WindowsOcrEngine(OcrEngine):
    name = "windows"

    def __init__(self, language: str = "es-MX"):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine as _Win
        self._win = _Win
        self._engine = _Win.try_create_from_language(Language(language)) or _Win.try_create_from_user_profile_languages()
        if self._engine is None:
            raise RuntimeError("Windows OCR no tiene un idioma disponible.")
        self._max_dim = int(_Win.max_image_dimension)

    async def _recognize(self, png: bytes):
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(png)
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        return await self._engine.recognize_async(bitmap)

    def ocr(self, img: np.ndarray) -> OcrOutput:
        h, w = img.shape[:2]
        if max(h, w) > self._max_dim:
            f = self._max_dim / max(h, w)
            img = cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return OcrOutput()
        result = asyncio.run(self._recognize(buf.tobytes()))
        out = OcrOutput()
        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            rects = [w_.bounding_rect for w_ in words]
            x0 = min(r.x for r in rects); y0 = min(r.y for r in rects)
            x1 = max(r.x + r.width for r in rects); y1 = max(r.y + r.height for r in rects)
            out.lines.append(OcrLine(text=line.text, conf=None, box=(x0, y0, x1, y1)))
        return out


_engines: dict[str, OcrEngine] = {}
_engines_lock = threading.Lock()


def get_engine(settings: Optional[Settings] = None) -> OcrEngine:
    s = settings or get_settings()
    wanted = s.ocr_engine
    with _engines_lock:
        if wanted in _engines:
            return _engines[wanted]
        engine: Optional[OcrEngine] = None
        if wanted == "windows":
            try:
                engine = WindowsOcrEngine()
            except Exception:
                engine = None  # se cae a rapidocr y se informa por engine.name
        if engine is None:
            engine = _engines.get("rapidocr") or RapidOcrEngine()
            _engines["rapidocr"] = engine
        _engines[wanted] = engine
        return engine
