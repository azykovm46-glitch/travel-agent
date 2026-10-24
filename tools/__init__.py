"""Интерфейс инструментов.

Агент вызывает инструменты только через get_backend(). Сейчас это моки
(tools/mock_backend.py). Когда участник 2 сделает реальный бэкенд, он
кладёт модуль с теми же функциями и сигнатурами и переключает
переменную окружения TOOLS_BACKEND, например TOOLS_BACKEND=tools.real_backend.
Контракт функций описан в docs/CONTRACTS.md.
"""
from __future__ import annotations

import importlib
import os
from functools import lru_cache

REQUIRED_FUNCTIONS = [
    "search_destinations", "get_weather", "search_transport", "search_hotels",
    "search_transfers", "checkout", "get_destination", "find_destination_by_text",
    "normalize_origin",
]


@lru_cache(maxsize=1)
def get_backend():
    name = os.getenv("TOOLS_BACKEND", "tools.mock_backend")
    module = importlib.import_module(name)
    missing = [f for f in REQUIRED_FUNCTIONS if not hasattr(module, f)]
    if missing:
        raise RuntimeError(f"Бэкенд {name} не реализует: {', '.join(missing)}")
    return module
