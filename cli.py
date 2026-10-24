"""Чат с агентом в терминале — самый быстрый способ отлаживать логику.

    python cli.py            # с LLM, если задан LLM_API_KEY в .env
    python cli.py --offline  # только правила и шаблоны
    python cli.py --trace    # показывать шаги агента

Команды: /state — показать TripState, /reset — новая сессия, /quit — выход.
Цифра или текст кнопки из списка «Кнопки» — нажать быструю кнопку.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def load_env() -> None:
    env = Path(__file__).with_name(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def show(resp, trace: bool) -> list:
    print(f"\n\033[1mАгент [{resp.stage}]\033[0m: {resp.message}")
    for q in resp.questions:
        print(f"  ? {q.text}  " + " | ".join(o.label for o in q.options))
    for c in resp.cards:
        d = c.data
        if c.type == "destination":
            print(f"  ◆ {d['rank']}. {d['name']} ({d['country']}): воздух {d['air_temp']}°, вода {d['water_temp']}°, "
                  f"{d['flight_hours']} ч, ~{d['est_total_rub']:,} ₽".replace(",", " "))
        elif c.type == "plan":
            print(f"  ▣ План: {d['destination']['name']} {d['check_in']}–{d['check_out']}, "
                  f"{d['outbound']['carrier']} {d['outbound']['departure'][11:]} → {d['hotel']['name']}, "
                  f"итого {d['total_rub']:,} ₽".replace(",", " "))
            e = d["hotel"]["explanation"]
            print(f"    почему: {'; '.join(e['why'])}\n    жертвуем: {'; '.join(e['tradeoffs']) or '—'}\n    риски: {'; '.join(e['risks']) or '—'}")
        elif c.type == "issues":
            for i in d["items"]:
                print(f"  ! [{i['level']}] {i['message']}")
        elif c.type == "assumptions" and d["items"]:
            print("  ~ Допущения: " + "; ".join(f"{a['value']} ({a['reason']})" for a in d["items"]))
        elif c.type == "order":
            print(f"  ✓ Заказ {d['order_id']}: {d['paid_rub']:,} ₽".replace(",", " "))
    for row in resp.diff:
        print(f"  Δ {row['label']}: {row['before']} → {row['after']}")
    replies = list(resp.quick_replies) + [o for q in resp.questions for o in q.options]
    if replies:
        print("  Кнопки: " + "  ".join(f"[{i}] {r.label}" for i, r in enumerate(replies, 1)))
    if trace:
        for s in resp.trace:
            print(f"    · {s.step} ({s.ms} мс): {s.detail}")
    return replies


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--trace", action="store_true")
    args = ap.parse_args()
    load_env()
    if args.offline:
        os.environ["AGENT_OFFLINE"] = "1"
    from agent import TravelAgent
    agent = TravelAgent()
    sid, replies = None, []
    print("Опишите поездку своими словами. /state, /reset, /quit")
    while True:
        try:
            text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text in ("/quit", "/q"):
            break
        if text == "/reset":
            sid, replies = None, []
            continue
        if text == "/state" and sid:
            print(json.dumps(agent.get_state(sid).model_dump(mode="json", exclude={"candidates"}), ensure_ascii=False, indent=2))
            continue
        msg, action = text, None
        if text.isdigit() and 0 < int(text) <= len(replies):
            r = replies[int(text) - 1]
            msg, action = r.text, r.action
        resp = agent.handle(sid, message=msg, action=action)
        sid = resp.session_id
        replies = show(resp, args.trace)
    sys.exit(0)


if __name__ == "__main__":
    main()
