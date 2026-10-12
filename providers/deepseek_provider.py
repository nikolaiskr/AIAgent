from __future__ import annotations

from .openai_provider import OpenAITextProvider


class DeepSeekTextProvider(OpenAITextProvider):
    @property
    def name(self) -> str:
        return f"DeepSeek:{self.model}"
