from __future__ import annotations

import json
import tempfile
from pathlib import Path

from providers.router import HybridRouter
from settings import Settings

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None


class VisionError(RuntimeError):
    pass


IMAGE_PROMPT = """
Проанализируй изображение как материал учебной работы. Извлеки видимый текст максимально точно.
Отдельно укажи, если на изображении есть формула, таблица, график, схема или диаграмма.
Не додумывай нечитаемые значения: помечай их как [НЕРАЗБОРЧИВО].
Верни обычный структурированный текст на русском языке, пригодный для дальнейшего retrieval.
""".strip()


def analyze_image(settings: Settings, path: Path) -> str:
    provider = HybridRouter(settings).vision()
    if provider is None:
        raise VisionError("OpenAI Vision не настроен.")
    return provider.analyze_image(path, IMAGE_PROMPT, max_tokens=6000)


def analyze_pdf_scan(settings: Settings, path: Path) -> tuple[str, list[str]]:
    """Рендерит страницы сканированного PDF и отправляет их vision-модели."""
    if fitz is None:
        raise VisionError("Для PDF vision установи PyMuPDF из requirements.txt.")
    provider = HybridRouter(settings).vision()
    if provider is None:
        raise VisionError("OpenAI Vision не настроен.")

    warnings: list[str] = []
    blocks: list[str] = []
    try:
        pdf = fitz.open(path)
    except Exception as exc:
        raise VisionError(f"Не удалось открыть PDF для vision: {exc}") from exc

    pages = min(len(pdf), settings.max_vision_pages)
    if len(pdf) > pages:
        warnings.append(f"Vision обработал первые {pages} из {len(pdf)} страниц. Увеличь MAX_VISION_PAGES при необходимости.")

    with tempfile.TemporaryDirectory(prefix="aiagent_pdfvision_") as tmpdir:
        for i in range(pages):
            page = pdf[i]
            pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
            img = Path(tmpdir) / f"page_{i+1:03d}.jpg"
            pix.save(str(img))
            try:
                text = provider.analyze_image(
                    img,
                    f"Это страница {i+1} PDF-файла {path.name}.\n\n{IMAGE_PROMPT}",
                    max_tokens=6000,
                )
                blocks.append(f"\n--- VISION СТРАНИЦА {i+1} ---\n{text}\n")
            except Exception as exc:
                warnings.append(f"Страница {i+1}: vision не выполнен: {exc}")
    pdf.close()
    return "".join(blocks).strip(), warnings
