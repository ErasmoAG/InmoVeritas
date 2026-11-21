"""Configuracion central. Lee variables de entorno (y .env si existe).

No se incluyen claves en el codigo. Los limites son configurables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Cargador minimo de .env (sin dependencias). No sobreescribe variables ya definidas."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    storage_dir: Path = field(default_factory=lambda: _resolve(os.environ.get("INMO_STORAGE_DIR", "storage")))
    rules_file: Path = field(default_factory=lambda: _resolve(os.environ.get("INMO_RULES_FILE", "config/rules_config.yaml")))
    max_file_bytes: int = field(default_factory=lambda: _int("MAX_FILE_MB", 60) * 1024 * 1024)
    max_files_per_submission: int = field(default_factory=lambda: _int("MAX_FILES_PER_SUBMISSION", 40))
    max_pages_per_doc: int = field(default_factory=lambda: _int("MAX_PAGES_PER_DOC", 120))      # recepcion
    max_ocr_pages_per_doc: int = field(default_factory=lambda: _int("MAX_OCR_PAGES_PER_DOC", 30))  # analisis
    ocr_engine: str = field(default_factory=lambda: os.environ.get("OCR_ENGINE", "rapidocr").lower())  # rapidocr | windows
    ocr_dpi: int = field(default_factory=lambda: _int("OCR_DPI", 150))
    ocr_max_side_px: int = field(default_factory=lambda: _int("OCR_MAX_SIDE_PX", 2200))
    native_text_min_chars: int = field(default_factory=lambda: _int("NATIVE_TEXT_MIN_CHARS", 80))
    # Umbrales de calidad de imagen (configurables; NO son prueba definitiva de legibilidad)
    q_blur_warn: float = field(default_factory=lambda: _float("QUALITY_BLUR_WARN", 120.0))
    q_blur_critical: float = field(default_factory=lambda: _float("QUALITY_BLUR_CRITICAL", 40.0))
    q_contrast_warn: float = field(default_factory=lambda: _float("QUALITY_CONTRAST_WARN", 100.0))
    q_contrast_critical: float = field(default_factory=lambda: _float("QUALITY_CONTRAST_CRITICAL", 45.0))
    q_dark_warn: float = field(default_factory=lambda: _float("QUALITY_DARK_WARN", 90.0))
    q_dark_critical: float = field(default_factory=lambda: _float("QUALITY_DARK_CRITICAL", 50.0))
    q_min_side_warn: int = field(default_factory=lambda: _int("QUALITY_MIN_SIDE_WARN", 1000))
    q_min_side_critical: int = field(default_factory=lambda: _int("QUALITY_MIN_SIDE_CRITICAL", 500))
    ocr_min_confidence: float = field(default_factory=lambda: _float("OCR_MIN_CONFIDENCE", 0.6))
    zip_max_uncompressed_bytes: int = field(default_factory=lambda: _int("ZIP_MAX_UNCOMPRESSED_MB", 300) * 1024 * 1024)
    zip_max_files: int = field(default_factory=lambda: _int("ZIP_MAX_FILES", 60))
    zip_max_ratio: int = field(default_factory=lambda: _int("ZIP_MAX_RATIO", 100))
    min_image_side_px: int = field(default_factory=lambda: _int("MIN_IMAGE_SIDE_PX", 300))
    ollama_base_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct"))
    llm_timeout_seconds: int = field(default_factory=lambda: _int("LLM_TIMEOUT_SECONDS", 45))


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def get_settings() -> Settings:
    return Settings()
