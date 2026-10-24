"""Проиграть демо-сценарии через API — для репетиции защиты и записи резервного видео.

    python scripts/demo.py                         # внутри процесса, офлайн (правила + шаблоны)
    python scripts/demo.py --llm                   # с LLM из .env
    python scripts/demo.py --url http://localhost:8000   # против запущенного uvicorn
    python scripts/demo.py --only 2                # один сценарий

Каждый шаг проверяет ожидаемый этап (stage). Код выхода 1, если что-то пошло не так.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIRST = {"pick": 0}          # нажать первую быструю кнопку из предыдущего ответа

SCENARIOS = [
    ("Семья к тёплому морю: один запрос → бронь → пересборка → ранний заезд", [
        ("хочу к тёплому морю в ноябре, недорого, с ребёнком 5 лет, без долгих перелётов, из Москвы", "inspire"),
        (FIRST, "plan"),
        ("а если на 2 дня позже и бюджет меньше на 20%", "plan"),
        ({"type": "checkout"}, "booked"),
        ("можно заселиться пораньше?", "booked"),
    ]),
    ("Горнолыжка: правильные направления, длинный трансфер без ошибок стыковки", [
        ("в горы на лыжах в феврале вдвоём из Москвы", "inspire"),
        (FIRST, "plan"),
        ("давай другой отель", "plan"),
        ({"type": "checkout"}, "booked"),
    ]),
    ("Пересборка без поиска с нуля: собака, без пересадок, ближе к морю", [
        ("Сочи на неделю в ноябре вдвоём из Москвы", "plan"),
        ("с нами едет собака", "plan"),
        ("а можно без пересадок?", "plan"),
        ("хочу отель ближе к морю", "plan"),
        ({"type": "checkout"}, "booked"),
    ]),
    ("Размытый запрос: один раунд уточнений, дальше допущения", [
        ("хочу куда-нибудь отдохнуть", "clarify"),
        ("из Петербурга в декабре на 5 ночей вдвоём", "inspire"),
        (FIRST, "plan"),
        ({"type": "checkout"}, "booked"),
    ]),
    ("Сложная семья и смена после брони: 5 человек, точные даты, другой город", [
        ("из Владивостока в Анталью с 28 декабря по 3 января, мы вдвоём и трое детей 4, 8 и 12 лет", "plan"),
        ("а в Шарм?", "plan"),
        ({"type": "checkout"}, "booked"),
        ("на день позже", "plan"),
        ({"type": "checkout"}, "booked"),
    ]),
]


def make_client(url: str | None, llm: bool):
    if url:
        import httpx
        return httpx.Client(base_url=url, timeout=60)
    if not llm:
        os.environ["AGENT_OFFLINE"] = "1"
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def run_scenario(client, title: str, steps: list) -> bool:
    print(f"\n\033[1m=== {title}\033[0m")
    sid, last, ok = None, None, True
    for what, expected in steps:
        body = {"session_id": sid}
        if isinstance(what, str):
            body["message"] = what
            shown = what
        elif "pick" in what:
            reply = last["quick_replies"][what["pick"]]
            body.update({"message": reply.get("text"), "action": reply.get("action")})
            shown = f"[кнопка] {reply['label']}"
        else:
            body["action"] = what
            shown = f"[действие] {what['type']}"
        t0 = time.perf_counter()
        resp = client.post("/api/chat", json=body)
        ms = int((time.perf_counter() - t0) * 1000)
        if resp.status_code != 200:
            print(f"> {shown}\n  ✗ HTTP {resp.status_code}: {resp.text[:200]}")
            return False
        last = resp.json()
        sid = last["session_id"]
        mark = "✓" if last["stage"] == expected else f"✗ ждали {expected}"
        ok &= last["stage"] == expected
        print(f"> {shown}\n  {mark} [{last['stage']}] {ms} мс: {last['message']}")
        for d in last["diff"]:
            print(f"    Δ {d['label']}: {d['before']} → {d['after']}")
        issues = next((c["data"]["items"] for c in last["cards"] if c["type"] == "issues"), [])
        for i in issues:
            if i["level"] != "info":
                print(f"    ! {i['message']}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="адрес запущенного API, например http://localhost:8000")
    ap.add_argument("--llm", action="store_true", help="с LLM из .env (по умолчанию офлайн)")
    ap.add_argument("--only", type=int, help="номер сценария с 1")
    args = ap.parse_args()
    from cli import load_env
    load_env()
    client = make_client(args.url, args.llm)
    chosen = [SCENARIOS[args.only - 1]] if args.only else SCENARIOS
    results = [run_scenario(client, title, steps) for title, steps in chosen]
    print(f"\nИтог: {sum(results)}/{len(results)} сценариев прошли")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
