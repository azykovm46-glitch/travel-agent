"""Прогон тестовых запросов и метрики для слайда.

    python evals/run.py              # с LLM, если настроена
    python evals/run.py --offline    # только правила

Симулированный пользователь: отвечает на уточнения первой кнопкой,
выбирает первое направление, затем оформляет. Метрики:
  completion   — доля сценариев, дошедших до брони за <= 6 ходов
  clarify_avg  — среднее число уточняющих вопросов
  first_rec_s  — время до первой рекомендации (карточки направления или плана)
  stitch_err   — число ошибок стыковок (level=error) в итоговых планах
  explained    — доля планов с непустым «почему»
  correct      — доля сценариев с полем expect, где агент понял запрос правильно

Поле expect в queries.jsonl (все ключи необязательны):
  top_dest_in / top_dest_not — id направлений, допустимые / недопустимые на первом месте
  dest_not                   — id направлений, которых не должно быть в тройке
  children                   — сколько детей должен понять агент
  origin, destination        — город вылета / id выбранного направления
  check_in_mmdd              — дата заезда «MM-DD»
  first_message_contains     — подстрока в первом ответе агента
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(expect: dict, first, final) -> list[str]:
    """Возвращает список несбывшихся ожиданий."""
    fails = []
    dests = [c.data["id"] for c in first.cards if c.type == "destination"]
    state, plan = final.state, final.state.plan or {}
    if "top_dest_in" in expect and (not dests or dests[0] not in expect["top_dest_in"]):
        fails.append(f"первое направление {dests[:1]} не из {expect['top_dest_in']}")
    if "top_dest_not" in expect and dests and dests[0] in expect["top_dest_not"]:
        fails.append(f"первое направление {dests[0]}")
    for d in expect.get("dest_not", []):
        if d in dests:
            fails.append(f"в тройке есть {d}")
    if "children" in expect and len(state.travelers.children_ages) != expect["children"]:
        fails.append(f"детей {len(state.travelers.children_ages)} вместо {expect['children']}")
    if "origin" in expect and state.origin_city != expect["origin"]:
        fails.append(f"вылет {state.origin_city} вместо {expect['origin']}")
    if "destination" in expect and state.selection.destination_id != expect["destination"]:
        fails.append(f"направление {state.selection.destination_id} вместо {expect['destination']}")
    if "check_in_mmdd" in expect and plan.get("check_in", "")[5:] != expect["check_in_mmdd"]:
        fails.append(f"заезд {plan.get('check_in')} вместо …-{expect['check_in_mmdd']}")
    if "first_message_contains" in expect and expect["first_message_contains"] not in first.message:
        fails.append(f"в первом ответе нет «{expect['first_message_contains']}»")
    return fails


def run(offline: bool) -> dict:
    from cli import load_env
    load_env()
    if offline:
        os.environ["AGENT_OFFLINE"] = "1"
    from agent import TravelAgent

    rows = []
    for line in (ROOT / "evals" / "queries.jsonl").read_text(encoding="utf-8").splitlines():
        q = json.loads(line)
        agent = TravelAgent()
        t0, first_rec, questions, turns, err = time.perf_counter(), None, 0, 0, None
        resp = first = agent.handle(message=q["text"])
        try:
            while turns < 6:
                turns += 1
                questions += len(resp.questions)
                if first_rec is None and any(c.type in ("destination", "plan") for c in resp.cards):
                    first_rec = time.perf_counter() - t0
                if resp.stage == "booked":
                    break
                if resp.questions:
                    resp = agent.handle(resp.session_id, message=resp.questions[0].options[0].text)
                elif resp.stage == "inspire":
                    resp = agent.handle(resp.session_id, action=resp.quick_replies[0].action)
                elif resp.stage == "plan" and resp.state.plan:
                    resp = agent.handle(resp.session_id, action={"type": "checkout"})
                else:
                    resp = agent.handle(resp.session_id, message="давай любой вариант")
        except Exception as e:      # падение = несработавший сценарий, но прогон продолжается
            err = repr(e)
        plan = resp.state.plan or {}
        rows.append({
            "id": q["id"], "booked": resp.stage == "booked", "turns": turns, "questions": questions,
            "first_rec_s": round(first_rec, 3) if first_rec is not None else None,
            "stitch_errors": sum(1 for i in plan.get("issues", []) if i["level"] == "error"),
            "explained": bool(plan.get("hotel", {}).get("explanation", {}).get("why")),
            "expect_fails": check(q["expect"], first, resp) if "expect" in q else None,
            "error": err,
        })
    recs = [r["first_rec_s"] for r in rows if r["first_rec_s"] is not None]
    planned = [r for r in rows if r["booked"]]
    checked = [r for r in rows if r["expect_fails"] is not None]
    summary = {
        "n": len(rows),
        "completion": round(len(planned) / len(rows), 2),
        "clarify_avg": round(statistics.mean(r["questions"] for r in rows), 2),
        "first_rec_s_median": round(statistics.median(recs), 3) if recs else None,
        "stitch_err": sum(r["stitch_errors"] for r in rows),
        "explained": round(sum(r["explained"] for r in planned) / max(1, len(planned)), 2),
        "correct": round(sum(not r["expect_fails"] for r in checked) / len(checked), 2) if checked else None,
    }
    return {"summary": summary, "rows": rows}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    res = run(ap.parse_args().offline)
    for r in res["rows"]:
        print(f"{'✓' if r['booked'] else '✗'} {r['id']:<22} ходов {r['turns']}  вопросов {r['questions']}  "
              f"первая рек. {r['first_rec_s']}с  ошибок стыковки {r['stitch_errors']}" + (f"  ОШИБКА {r['error']}" if r["error"] else "")
              + (f"\n    ✗ не понял: {'; '.join(r['expect_fails'])}" if r["expect_fails"] else ""))
    print("\nИТОГО:", json.dumps(res["summary"], ensure_ascii=False))
