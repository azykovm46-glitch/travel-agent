"""Проверка подключения LLM: работает ли модель, в каком режиме и чем её разбор отличается от правил.

    python scripts/check_llm.py

Берёт LLM_* из .env. Печатает для каждого запроса режим, время, патч модели и
расхождения с rule_patch. Код выхода 1, если модель не ответила ни разу.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cli import load_env  # noqa: E402

load_env()

from agent.llm import LLM  # noqa: E402
from agent.state import TripState, summarize  # noqa: E402
from agent.understand import rule_patch  # noqa: E402

QUERIES = [
    "хочу к тёплому морю в ноябре, недорого, с ребёнком 5 лет, без долгих перелётов, из Москвы",
    "хачу на мори в дикабре с жиной недорага",
    "beach trip in november for two from Moscow, cheap",
    "с 28 декабря по 3 января в горы на лыжах, нас трое, из Екатеринбурга, бюджет 200к",
    "какая погода на марсе?",
]


def main() -> int:
    llm = LLM()
    if not llm.enabled:
        print("LLM выключена: впишите LLM_API_KEY и LLM_MODEL в .env (и AGENT_OFFLINE=0).")
        return 1
    print(f"Модель: {llm.model} (запасная: {llm.fallback_model or '—'}), {llm.base_url}\n")
    today, ok = date.today(), 0
    for q in QUERIES:
        state = TripState()
        t0 = time.perf_counter()
        patch = llm.extract_patch(q, summarize(state), today.isoformat())
        ms = int((time.perf_counter() - t0) * 1000)
        rules = rule_patch(q, state, today)
        print(f"> {q}")
        if not patch:
            print(f"  ✗ нет патча ({ms} мс): {llm.last_error}\n")
            continue
        ok += 1
        print(f"  режим {llm.mode}, {ms} мс\n  LLM:     {json.dumps(patch, ensure_ascii=False)}")
        diff = {k: (rules.get(k), patch.get(k)) for k in sorted(set(rules) | set(patch)) if rules.get(k) != patch.get(k)}
        for k, (r, m) in diff.items():
            print(f"  ≠ {k}: правила {r!r} / LLM {m!r}")
        print()
    text = llm.write({"stage": "plan", "destination": "Сочи", "dates": "14.11–21.11", "total_rub": 77728,
                      "hotel": {"name": "Лазурь 3*", "why": ["50 м до пляжа", "оценка 8.1"]}})
    print(f"Проверка write(): {text or '✗ ' + str(llm.last_error)}")
    print(f"\nИтог: {ok}/{len(QUERIES)} патчей, рабочий режим: {llm.mode}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
