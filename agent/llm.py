"""Клиент LLM. Любой OpenAI-совместимый API: OpenRouter, OpenAI, локальная модель
через vLLM/Ollama и т.п. Модель меняется переменными окружения, код — нет.

Если ключа нет или задан AGENT_OFFLINE=1, агент работает на правилах
(agent/understand.py) и шаблонах (agent/explain.py) — это резерв для демо и тестов.
"""
from __future__ import annotations

import json
import logging
import os

from agent.prompts import EXTRACT_SYSTEM, UPDATE_TRIP_STATE_TOOL, WRITER_SYSTEM

log = logging.getLogger(__name__)


class LLM:
    def __init__(self) -> None:
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.base_url = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
        self.model = os.getenv("LLM_MODEL", "")
        self.fallback_model = os.getenv("LLM_FALLBACK_MODEL", "")
        self.timeout = float(os.getenv("LLM_TIMEOUT", "25"))
        self.enabled = bool(self.api_key and self.model) and os.getenv("AGENT_OFFLINE") != "1"
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI   # импорт здесь, чтобы офлайн-режим работал без пакета
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
        return self._client

    def _chat(self, **kwargs):
        last_err = None
        for model in [m for m in (self.model, self.fallback_model) if m]:
            try:
                return self.client.chat.completions.create(model=model, **kwargs)
            except Exception as e:     # сеть, лимиты, неверная модель — пробуем запасную
                last_err = e
                log.warning("LLM %s failed: %s", model, e)
        raise RuntimeError(f"LLM недоступна: {last_err}")

    def extract_patch(self, message: str, state_summary: str, today: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            resp = self._chat(
                messages=[
                    {"role": "system", "content": EXTRACT_SYSTEM + f"\nСегодня {today}."},
                    {"role": "user", "content": f"Текущее состояние поездки: {state_summary}\n\nСообщение пользователя: {message}"},
                ],
                tools=[UPDATE_TRIP_STATE_TOOL],
                tool_choice={"type": "function", "function": {"name": "update_trip_state"}},
                temperature=0,
            )
            calls = resp.choices[0].message.tool_calls or []
            return json.loads(calls[0].function.arguments) if calls else None
        except Exception as e:
            log.warning("extract_patch fallback to rules: %s", e)
            return None

    def write(self, facts: dict) -> str | None:
        if not self.enabled:
            return None
        try:
            resp = self._chat(
                messages=[
                    {"role": "system", "content": WRITER_SYSTEM},
                    {"role": "user", "content": json.dumps(facts, ensure_ascii=False, default=str)},
                ],
                temperature=0.3,
                max_tokens=350,
            )
            return (resp.choices[0].message.content or "").strip() or None
        except Exception as e:
            log.warning("write fallback to template: %s", e)
            return None
