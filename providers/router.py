from __future__ import annotations

from settings import Settings

from .base import ProviderError, TextProvider
from .deepseek_provider import DeepSeekTextProvider
from .openai_provider import OpenAITextProvider


class HybridRouter:
    """DeepSeek = объём/основной writer; OpenAI = document intelligence, vision и quality."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def writer(self) -> TextProvider:
        if self.settings.deepseek_ready:
            return DeepSeekTextProvider(
                api_key=self.settings.deepseek_api_key or "",
                model=self.settings.deepseek_model or "",
                base_url=self.settings.deepseek_base_url,
            )
        if self.settings.openai_ready:
            return self.quality_required()
        raise ProviderError(
            "AI ещё не настроен. Заполни DEEPSEEK_API_KEY+DEEPSEEK_MODEL или OPENAI_API_KEY+OPENAI_MODEL."
        )

    def quality(self) -> OpenAITextProvider | None:
        if not self.settings.openai_ready:
            return None
        return self.quality_required()

    def quality_required(self) -> OpenAITextProvider:
        if not self.settings.openai_ready:
            raise ProviderError("Для этой операции нужен OpenAI API.")
        return OpenAITextProvider(
            api_key=self.settings.openai_api_key or "",
            model=self.settings.openai_model or "",
            base_url=self.settings.openai_base_url,
        )

    def vision(self) -> OpenAITextProvider | None:
        if not self.settings.vision_ready:
            return None
        return OpenAITextProvider(
            api_key=self.settings.openai_api_key or "",
            model=self.settings.openai_vision_model or self.settings.openai_model or "",
            base_url=self.settings.openai_base_url,
        )

    def reviewer(self) -> TextProvider:
        if self.settings.use_quality_review and self.settings.openai_ready:
            return self.quality_required()
        return self.writer()

    def editor(self) -> TextProvider:
        if self.settings.openai_ready:
            return self.quality_required()
        return self.writer()

    def status_lines(self) -> list[str]:
        return [
            f"OpenAI: {'✅ настроен' if self.settings.openai_ready else '⚪ не настроен'}",
            f"DeepSeek: {'✅ настроен' if self.settings.deepseek_ready else '⚪ не настроен'}",
            f"Vision: {'✅ готов' if self.settings.vision_ready else '⚪ не готов'}",
            "Маршрутизация: DeepSeek → основной текст; OpenAI → анализ файлов/vision/review; Python → DOCX/XLSX/графики.",
        ]


__all__ = ["HybridRouter", "ProviderError"]
