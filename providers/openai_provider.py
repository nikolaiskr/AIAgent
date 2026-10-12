from __future__ import annotations

import base64
import logging
import mimetypes
import time
from pathlib import Path

from openai import APIConnectionError, APITimeoutError, InternalServerError, OpenAI, RateLimitError

import usage
from .base import OutputTruncatedError, ProviderError, TextProvider

logger = logging.getLogger("aiagent.provider")

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (2, 4, 8, 16)


class OpenAITextProvider(TextProvider):
    def __init__(self, *, api_key: str, model: str, base_url: str | None = None) -> None:
        kwargs: dict[str, object] = {"api_key": api_key, "timeout": 240.0}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model

    @property
    def name(self) -> str:
        return f"OpenAI:{self.model}"

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = 0.15,
    ) -> str:
        payload: dict[str, object] = {"model": self.model, "messages": messages}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        return self._call(payload)

    def analyze_image(
        self,
        image_path: Path,
        prompt: str,
        *,
        max_tokens: int = 5000,
        detail: str = "high",
    ) -> str:
        """Vision-анализ локального изображения через data URL."""
        try:
            raw = image_path.read_bytes()
        except OSError as exc:
            raise ProviderError(f"Не удалось прочитать изображение {image_path.name}: {exc}") from exc
        mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(raw).decode("ascii")
        data_url = f"data:{mime};base64,{encoded}"
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": detail}},
                    ],
                }
            ],
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        return self._call(payload)

    def _call(self, payload: dict[str, object]) -> str:
        attempt = 0
        while True:
            try:
                response = self.client.chat.completions.create(**payload)
                self._log_usage(response)
                self._check_truncation(response)
                if not response.choices:
                    raise ProviderError("Модель вернула пустой список ответов.")
                content = response.choices[0].message.content
                if not content or not str(content).strip():
                    raise ProviderError("Модель вернула пустой ответ.")
                return str(content).strip()
            except (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError) as exc:
                attempt += 1
                if attempt >= MAX_ATTEMPTS:
                    raise ProviderError(
                        f"API недоступен после {MAX_ATTEMPTS} попыток (rate-limit/сеть/5xx): {exc}"
                    ) from exc
                delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                logger.warning("Повторная попытка API через %sс (%s/%s): %s", delay, attempt + 1, MAX_ATTEMPTS, exc)
                time.sleep(delay)
            except OutputTruncatedError:
                raise
            except ProviderError:
                raise
            except Exception as first_exc:
                # Совместимость с провайдерами, требующими max_completion_tokens / без temperature.
                message = str(first_exc).casefold()
                retry = dict(payload)
                changed = False
                if "max_tokens" in message and "max_completion_tokens" in message and "max_tokens" in retry:
                    retry["max_completion_tokens"] = retry.pop("max_tokens")
                    changed = True
                if "temperature" in message and "unsupported" in message and "temperature" in retry:
                    retry.pop("temperature", None)
                    changed = True
                if not changed:
                    raise ProviderError(f"Ошибка OpenAI-compatible API: {first_exc}") from first_exc
                try:
                    response = self.client.chat.completions.create(**retry)
                    self._log_usage(response)
                    self._check_truncation(response)
                except OutputTruncatedError:
                    raise
                except Exception as second_exc:
                    raise ProviderError(f"Ошибка OpenAI-compatible API: {second_exc}") from second_exc
                if not response.choices:
                    raise ProviderError("Модель вернула пустой список ответов.")
                content = response.choices[0].message.content
                if not content or not str(content).strip():
                    raise ProviderError("Модель вернула пустой ответ.")
                return str(content).strip()

    def _log_usage(self, response: object) -> None:
        usage_obj = getattr(response, "usage", None)
        if usage_obj is None:
            return
        try:
            usage.log_usage(
                self.model,
                prompt_tokens=int(getattr(usage_obj, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage_obj, "completion_tokens", 0) or 0),
            )
        except Exception:
            pass

    @staticmethod
    def _check_truncation(response: object) -> None:
        choices = getattr(response, "choices", None)
        if not choices:
            return
        finish = str(getattr(choices[0], "finish_reason", "") or "")
        if finish == "length":
            raise OutputTruncatedError(
                "Модель обрезала ответ по лимиту токенов (finish_reason='length')."
            )
