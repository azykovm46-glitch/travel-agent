"""Клиент LLM. Любой OpenAI-совместимый API: OpenRouter, OpenAI, локальная модель
через vLLM/Ollama и т.п. Модель меняется переменными окружения, код — нет.

Если ключа нет или задан AGENT_OFFLINE=1, агент работает на правилах
(agent/understand.py) и шаблонах (agent/explain.py) — это резерв для демо и тестов.

Разбор запроса пробует режимы от строгого к простому и запоминает рабочий:
  tools — принудительный вызов инструмента (лучший вариант, умеют не все модели);
  json  — response_format json_object + схема в промпте;
  text  — схема в промпте, JSON вырезаем из текста ответа.
Запасная модель (LLM_FALLBACK_MODEL) может жить у другого провайдера: LLM_FALLBACK_BASE_URL и
LLM_FALLBACK_API_KEY; не заданы — она идёт к основному. На 503 (провайдер отвечает им сразу) модель
переспрашиваем LLM_RETRIES раз с паузой LLM_RETRY_PAUSE; на таймаут — сразу к запасной: повтор того же
молчащего провайдера стоил бы ещё LLM_TIMEOUT. Весь запрос с повторами и запасной укладывается в
LLM_DEADLINE (30 с): каждая попытка получает не больше оставшегося времени. Повторы SDK openai выключены.
Если API лежит (сеть, таймаут, лимит), LLM отключается на LLM_COOLDOWN секунд (по умолчанию 60),
чтобы на демо каждый ход не ждал таймаута, — агент продолжает на правилах. evals/run.py ставит 0:
иначе прогон «с LLM» после первого сбоя минуту мерит правила.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date

from agent.prompts import EXTRACT_SYSTEM, JSON_MODE_SUFFIX, UPDATE_TRIP_STATE_TOOL, WRITER_SYSTEM

log = logging.getLogger(__name__)

MODES = ("tools", "json", "text")
UNSUPPORTED = {400, 404, 422}      # модель не умеет режим/параметр — пробуем режим попроще
COOLDOWN_S = 60                    # по умолчанию; LLM_COOLDOWN переопределяет
TRANSIENT = {503}                  # провайдер на секунду недоступен (Polza: «не удалось проверить лимиты»)
SCHEMA = UPDATE_TRIP_STATE_TOOL["function"]["parameters"]["properties"]


def _coerce(value, spec: dict):
    """Привести значение к типу из схемы; None — если не получается или вне допустимого."""
    if value is None:
        return None
    kind = spec.get("type")
    try:
        if kind == "integer":
            if isinstance(value, bool):
                return None
            v = int(float(value))
        elif kind == "number":
            if isinstance(value, bool):
                return None
            v = float(value)
        elif kind == "boolean":
            v = value if isinstance(value, bool) else {"true": True, "false": False}.get(str(value).lower())
        elif kind == "string":
            v = str(value).strip() or None
            if v and spec.get("format") == "date":
                date.fromisoformat(v)
        elif kind == "array":
            items = value if isinstance(value, list) else [value]
            v = [x for x in (_coerce(i, spec.get("items", {})) for i in items) if x is not None]
        else:
            v = value
    except (TypeError, ValueError):
        return None
    if v is None:
        return None
    if "enum" in spec and v not in spec["enum"]:
        return None
    if "minimum" in spec and v < spec["minimum"] or "maximum" in spec and v > spec["maximum"]:
        return None
    return v


def clean_patch(raw) -> dict | None:
    """Оставить только поля схемы с корректными значениями: LLM не должна ломать TripState."""
    if not isinstance(raw, dict):
        return None
    if "intent" not in raw:                    # некоторые модели заворачивают: {"name": ..., "arguments": {...}}
        inner = raw.get("arguments") or raw.get("parameters") or raw.get("patch")
        if isinstance(inner, dict):
            raw = inner
    patch = {}
    for key, value in raw.items():
        if key in SCHEMA and (v := _coerce(value, SCHEMA[key])) not in (None, [], ""):
            patch[key] = v
    return patch or None


def _json_from_text(text: str | None) -> dict | None:
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


class LLM:
    def __init__(self) -> None:
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.base_url = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
        self.model = os.getenv("LLM_MODEL", "")
        self.fallback_model = os.getenv("LLM_FALLBACK_MODEL", "")
        self.fallback_base_url = os.getenv("LLM_FALLBACK_BASE_URL", "")
        self.fallback_api_key = os.getenv("LLM_FALLBACK_API_KEY", "")
        self.timeout = float(os.getenv("LLM_TIMEOUT", "12"))
        self.deadline = float(os.getenv("LLM_DEADLINE", "30"))      # на один запрос со всеми повторами
        self.cooldown = float(os.getenv("LLM_COOLDOWN", str(COOLDOWN_S)))
        self.retries = min(2, max(0, int(os.getenv("LLM_RETRIES", "1"))))
        self.retry_pause = float(os.getenv("LLM_RETRY_PAUSE", "2"))
        # none | low | high — для «думающих» моделей; пусто — не отправляем, решает провайдер
        self.reasoning_effort = os.getenv("LLM_REASONING_EFFORT", "")
        self.enabled = bool(self.api_key and self.model) and os.getenv("AGENT_OFFLINE") != "1"
        self.mode = os.getenv("LLM_EXTRACT_MODE", "tools")   # с какого режима начинать
        if self.mode not in MODES:
            self.mode = "tools"
        self._client = None
        self._fallback_client = None
        self._down_until = 0.0
        self.last_error: str | None = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI   # импорт здесь, чтобы офлайн-режим работал без пакета
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout,
                                  max_retries=0)
        return self._client

    @property
    def fallback_client(self):
        """Клиент запасной модели: свой провайдер, если задан LLM_FALLBACK_BASE_URL, иначе основной."""
        if not self.fallback_base_url:
            return self.client
        if self._fallback_client is None:
            from openai import OpenAI
            self._fallback_client = OpenAI(api_key=self.fallback_api_key or self.api_key,
                                           base_url=self.fallback_base_url, timeout=self.timeout, max_retries=0)
        return self._fallback_client

    @property
    def available(self) -> bool:
        return self.enabled and time.monotonic() >= self._down_until

    def _chat(self, **kwargs):
        last_err = None
        if self.reasoning_effort:
            # не reasoning_effort: Polza его молча игнорирует, а рассуждения съедают max_tokens ответа
            kwargs["extra_body"] = {"reasoning": {"effort": self.reasoning_effort}}
        until = time.monotonic() + self.deadline
        targets = [(self.model, lambda: self.client)]
        if self.fallback_model:
            targets.append((self.fallback_model, lambda: self.fallback_client))
        for model, get_client in targets:
            if last_err is not None and until - time.monotonic() < 1:
                break                      # лимит на ход исчерпан — дальше правила
            try:
                return self._create(get_client(), model, kwargs, until)
            except Exception as e:     # сеть, лимиты, неверная модель — пробуем запасную
                # запасная не умеет режим, а основная просто перегружена: режим не понижаем, ждём основную
                if last_err is not None and getattr(e, "status_code", None) in UNSUPPORTED \
                        and getattr(last_err, "status_code", None) not in UNSUPPORTED:
                    log.warning("LLM %s не умеет этот режим: %s", model, e)
                    break
                last_err = e
                log.warning("LLM %s failed: %s", model, e)
        raise last_err

    def _create(self, client, model: str, kwargs: dict, until: float):
        """Запрос к одной модели; на 503 — повтор с паузой, если до лимита хода хватает времени."""
        for attempt in range(self.retries + 1):
            left = until - time.monotonic()
            try:
                return client.chat.completions.create(model=model, timeout=max(1.0, min(self.timeout, left)), **kwargs)
            except Exception as e:
                last = attempt == self.retries or until - time.monotonic() - self.retry_pause < 2
                if getattr(e, "status_code", None) not in TRANSIENT or last:
                    raise
                log.warning("LLM %s: %s — повтор через %s с", model, e, self.retry_pause)
                time.sleep(self.retry_pause)

    def _fail(self, e: Exception) -> None:
        self.last_error = str(e)[:120]          # видно в trace: LLM включена, но упала
        if getattr(e, "status_code", None) not in UNSUPPORTED and self.cooldown > 0:
            self._down_until = time.monotonic() + self.cooldown
            log.warning("LLM недоступна, %g с работаем на правилах: %s", self.cooldown, e)

    def _extract(self, mode: str, messages: list[dict]) -> dict | None:
        if mode == "tools":
            resp = self._chat(messages=messages, tools=[UPDATE_TRIP_STATE_TOOL], temperature=0,
                              tool_choice={"type": "function", "function": {"name": "update_trip_state"}})
            calls = resp.choices[0].message.tool_calls or []
            return _json_from_text(calls[0].function.arguments) if calls else None
        schema_msg = {"role": "system", "content": JSON_MODE_SUFFIX + json.dumps(
            UPDATE_TRIP_STATE_TOOL["function"]["parameters"], ensure_ascii=False)}
        extra = {"response_format": {"type": "json_object"}} if mode == "json" else {}
        resp = self._chat(messages=[messages[0], schema_msg, *messages[1:]], temperature=0, **extra)
        return _json_from_text(resp.choices[0].message.content)

    def extract_patch(self, message: str, state_summary: str, today: str) -> dict | None:
        if not self.enabled:
            return None
        if not self.available:
            self.last_error = "пауза после сбоя API"
            return None
        self.last_error = None
        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM + f"\nСегодня {today}."},
            {"role": "user", "content": f"Текущее состояние поездки: {state_summary}\n\nСообщение пользователя: {message}"},
        ]
        for mode in MODES[MODES.index(self.mode):]:
            try:
                patch = clean_patch(self._extract(mode, messages))
            except Exception as e:
                self._fail(e)
                if getattr(e, "status_code", None) in UNSUPPORTED:
                    continue
                return None
            if patch:
                if mode != self.mode:
                    log.warning("LLM: режим %s не сработал, дальше используем %s", self.mode, mode)
                    self.mode = mode
                self.last_error = None
                return patch
            self.last_error = f"режим {mode}: модель не вернула патч"
        return None

    def write(self, facts: dict) -> str | None:
        if not self.available:
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
            self._fail(e)
            return None
