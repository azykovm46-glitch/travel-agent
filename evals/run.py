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
  correct      — доля сценариев с полем expect, где все ожидания сбылись (= understood и in_data)
  understood   — доля, где агент понял запрос: не сбылось только то, чего нет в данных бэкенда,
                 и агент честно сказал об этом в первом ответе («нет данных», «не умею искать»…)
  in_data      — доля, где всё нужное нашлось в данных бэкенда
  llm_understand / llm_write — доля ходов, где разбор / текст реально дала LLM (без --offline;
                 при сбоях API агент молча уходит на правила, и метрики становятся офлайновыми)

Поле expect в queries.jsonl (все ключи необязательны):
  top_dest_in / top_dest_not — id направлений, допустимые / недопустимые на первом месте
  dest_not                   — id направлений, которых не должно быть в тройке
  children                   — сколько детей должен понять агент
  origin, destination        — город вылета / id выбранного направления
  destination_country        — страна выбранного направления (запрос назвал страну, а не город)
  check_in_mmdd              — дата заезда «MM-DD»
  month, adults, budget_level — что агент понял в итоговом состоянии
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


# Фразы, которыми агент честно говорит, что нужного нет в данных бэкенда
MISSING_MARKERS = ("нет данных", "не умею искать", "горнолыжных курортов")


def check(expect: dict, first, final) -> list[tuple[str, str]]:
    """Несбывшиеся ожидания: (вид, текст). Вид «data» — агент понял верно, но нужного места или города
    нет в данных бэкенда, и он честно сказал об этом в первом ответе. Иначе «understand» — не понял."""
    from tools import get_backend
    be = get_backend()
    honest = any(m in first.message.lower() for m in MISSING_MARKERS)

    def kind(missing: bool) -> str:
        return "data" if missing and honest else "understand"

    fails = []
    dests = [c.data["id"] for c in first.cards if c.type == "destination"]
    state, plan = final.state, final.state.plan or {}
    if "top_dest_in" in expect and (not dests or dests[0] not in expect["top_dest_in"]):
        fails.append((kind(not any(be.get_destination(d) for d in expect["top_dest_in"])),
                      f"первое направление {dests[:1]} не из {expect['top_dest_in']}"))
    if "top_dest_not" in expect and dests and dests[0] in expect["top_dest_not"]:
        fails.append(("understand", f"первое направление {dests[0]}"))
    for d in expect.get("dest_not", []):
        if d in dests:
            fails.append(("understand", f"в тройке есть {d}"))
    if "children" in expect and len(state.travelers.children_ages) != expect["children"]:
        fails.append(("understand", f"детей {len(state.travelers.children_ages)} вместо {expect['children']}"))
    if "origin" in expect and state.origin_city != expect["origin"]:
        fails.append((kind(not be.normalize_origin(expect["origin"].lower())),
                      f"вылет {state.origin_city} вместо {expect['origin']}"))
    if "destination" in expect and state.selection.destination_id != expect["destination"]:
        fails.append((kind(not be.get_destination(expect["destination"])),
                      f"направление {state.selection.destination_id} вместо {expect['destination']}"))
    if "destination_country" in expect:          # «Египет» без города — подходит любой курорт страны
        chosen = be.get_destination(state.selection.destination_id or "") or {}
        if chosen.get("country") != expect["destination_country"]:
            fails.append(("understand", f"направление {state.selection.destination_id} не в стране {expect['destination_country']}"))
    if "check_in_mmdd" in expect and plan.get("check_in", "")[5:] != expect["check_in_mmdd"]:
        fails.append(("understand", f"заезд {plan.get('check_in')} вместо …-{expect['check_in_mmdd']}"))
    for key, actual in (("month", state.dates.month or (state.dates.start and state.dates.start.month)),
                        ("adults", state.travelers.adults), ("budget_level", state.budget.level)):
        if key in expect and actual != expect[key]:
            fails.append(("understand", f"{key} {actual!r} вместо {expect[key]!r}"))
    if "first_message_contains" in expect and expect["first_message_contains"] not in first.message:
        # ожидаемой фразы нет, но агент сказал, что нужного нет в данных («не сезон» без курортов вообще)
        fails.append((kind(True), f"в первом ответе нет «{expect['first_message_contains']}»"))
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
        llm_turns = {"understand": [0, 0], "write": [0, 0]}     # [ходов с LLM, всего ходов шага]
        handle = agent.handle

        def counted(*a, **kw):
            r = handle(*a, **kw)
            for step in r.trace:
                if step.step in llm_turns:
                    llm_turns[step.step][1] += 1
                    llm_turns[step.step][0] += step.detail.startswith(("LLM", "текст от LLM"))
            return r

        agent.handle = counted
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
            "error": err, "llm_turns": llm_turns,
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
        # correct = understood и in_data одновременно
        "understood": round(sum(all(k != "understand" for k, _ in r["expect_fails"]) for r in checked)
                            / len(checked), 2) if checked else None,
        "in_data": round(sum(all(k != "data" for k, _ in r["expect_fails"]) for r in checked)
                         / len(checked), 2) if checked else None,
    }
    for step in ("understand", "write"):
        used, total = (sum(r["llm_turns"][step][i] for r in rows) for i in (0, 1))
        summary[f"llm_{step}"] = round(used / total, 2) if total else None   # доля ходов, где реально ответила LLM
    return {"summary": summary, "rows": rows}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    res = run(ap.parse_args().offline)
    for r in res["rows"]:
        print(f"{'✓' if r['booked'] else '✗'} {r['id']:<22} ходов {r['turns']}  вопросов {r['questions']}  "
              f"первая рек. {r['first_rec_s']}с  ошибок стыковки {r['stitch_errors']}" + (f"  ОШИБКА {r['error']}" if r["error"] else "")
              + "".join(f"\n    ✗ {'нет в данных' if k == 'data' else 'не понял'}: {t}" for k, t in r["expect_fails"] or []))
    print("\nИТОГО:", json.dumps(res["summary"], ensure_ascii=False))
