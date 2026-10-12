from __future__ import annotations

from abc import ABC, abstractmethod


class ProviderError(RuntimeError):
    pass


class OutputTruncatedError(ProviderError):
    """Модель обрезала ответ по лимиту токенов (finish_reason='length')."""


class TextProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = 0.15,
    ) -> str:
        raise NotImplementedError
