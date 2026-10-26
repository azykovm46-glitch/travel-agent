"""Сгенерировать docs/api_examples.json — реальные ответы /api/chat на каждом этапе.

    python scripts/make_api_examples.py

Фронтенд верстает по этому файлу, не дожидаясь бэкенда. Работает офлайн (без LLM),
ответы идут через FastAPI, поэтому формат ровно тот, что получит фронт.
Перегенерируйте после любого изменения формата ответа.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["AGENT_OFFLINE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

DEMO = "хочу к тёплому морю в ноябре, недорого, с ребёнком 5 лет, без долгих перелётов, из Москвы"


def main() -> None:
    client = TestClient(app)
    examples = []

    def call(name: str, note: str, body: dict) -> dict:
        resp = client.post("/api/chat", json=body)
        resp.raise_for_status()
        data = resp.json()
        examples.append({"name": name, "note": note, "request": body, "response": data})
        return data

    call("clarify", "Размытый запрос: агент задаёт ≤2 вопроса с кнопками-вариантами",
         {"session_id": None, "message": "хочу куда-нибудь отдохнуть", "action": None})
    r = call("inspire", "Запрос без конкретного места: 3 карточки направлений + допущения",
             {"session_id": None, "message": DEMO, "action": None})
    dest = next(c["data"]["id"] for c in r["cards"] if c["type"] == "destination")
    r = call("plan", "Выбор направления кнопкой: план, сравнение отелей и рейсов, проблемы стыковок",
             {"session_id": r["session_id"], "message": None, "action": {"type": "select_destination", "id": dest}})
    sid = r["session_id"]
    call("replan", "Смена условий: diff «было → стало», trace показывает, что переискано, а что сохранено",
         {"session_id": sid, "message": "а если на 2 дня позже и бюджет меньше на 20%", "action": None})
    call("booked", "Оформление: карточка order с номером брони (оплата имитирована)",
         {"session_id": sid, "message": None, "action": {"type": "checkout"}})
    call("unknown_place", "Место не из каталога: агент честно говорит об этом в message",
         {"session_id": None, "message": "хочу в Атлантиду из Москвы в декабре вдвоём", "action": None})

    # Тот же ход через WS /api/chat/ws: события шагов по мере выполнения, последним — response.
    # data в response — ровно AgentResponse из POST, здесь сокращён, чтобы не дублировать пример plan
    body = {"message": "Сочи в ноябре вдвоём из Москвы"}
    with client.websocket_connect("/api/chat/ws") as ws:
        ws.send_json(body)
        events = []
        while not events or events[-1]["type"] not in ("response", "error"):
            events.append(ws.receive_json())
    data = events[-1]["data"]
    events[-1]["data"] = {"session_id": data["session_id"], "stage": data["stage"], "message": data["message"],
                          "...": "остальные поля AgentResponse, как в примере plan"}
    examples.append({"name": "stream", "note": "WS /api/chat/ws: step_start → trace по каждому шагу → response",
                     "request": body, "events": events})

    out = ROOT / "docs" / "api_examples.json"
    out.write_text(json.dumps(examples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for e in examples:
        resp = e.get("response") or e["events"][-1]["data"]
        cards = ", ".join(c["type"] for c in resp.get("cards", []))
        print(f"{e['name']:<14} stage={resp['stage']:<8} cards=[{cards}]")
    print(f"\n→ {out.relative_to(ROOT)} ({out.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    main()
