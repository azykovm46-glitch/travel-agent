"""Объяснения: почему подходит / чем пожертвовали / риски.

Строятся только из полей данных и разбора скоринга, поэтому не врут.
LLM (если включена) лишь переписывает итоговое сообщение живым языком.
"""
from __future__ import annotations

import json
import re

from agent.state import TripState

MEAL_NAME = {"RO": "без питания", "BB": "завтраки", "HB": "завтрак и ужин", "AI": "всё включено"}


def _rub(x: int) -> str:
    return f"{x:,} ₽".replace(",", " ")


def _numbers(text: str) -> set[float]:
    # «175 375 ₽» -> 175375: склеиваем разряды, разделённые пробелами
    text = re.sub(r"(?<=\d)[\s  ](?=\d{3}(?!\d))", "", text)
    return {float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", text)}


def unsupported_numbers(text: str, facts: dict) -> list[str]:
    """Числа из текста LLM, которых нет во входных фактах: цены, часы, даты, проценты.
    Единицу пропускаем — «один из лучших» не факт."""
    allowed = _numbers(json.dumps(facts, ensure_ascii=False, default=str))
    return sorted(f"{n:g}" for n in _numbers(text) - allowed if n != 1)


def explain_hotel(h: dict, state: TripState, all_hotels: list[dict]) -> dict:
    bd, w = h["breakdown"], state.weights.normalized()
    sea = state.location_by_beach() and h.get("beach_m") is not None
    good = {
        "price": "один из самых выгодных среди подходящих",
        "location": f"{h['beach_m']} м до пляжа" if sea else f"{h['center_km']} км до центра",
        "rating": f"оценка {h['rating']} по {h['reviews']} отзывам",
        "cancellation": f"бесплатная отмена за {h['cancellation_deadline_days']} дн. до заезда",
        "meals": MEAL_NAME[h["meals"]],
        "travel_time": f"{h['airport_km']} км от аэропорта",
    }
    bad = {
        "price": "дороже других вариантов",
        "location": f"до пляжа {h['beach_m']} м" if sea else f"от центра {h['center_km']} км",
        "rating": f"оценка всего {h['rating']}",
        "cancellation": "без бесплатной отмены",
        "meals": MEAL_NAME[h["meals"]],
        "travel_time": f"далеко от аэропорта ({h['airport_km']} км)",
    }
    ranked = sorted(bd, key=lambda k: bd[k] * w[k], reverse=True)
    why = [good[k] for k in ranked if bd[k] >= 0.6][:3]
    if state.travelers.has_kids and (h["family_rooms"] or h["kids_club"]):
        why.append("есть семейные номера" + (" и детский клуб" if h["kids_club"] else ""))
    if h["review_pros"]:
        why.append("гости хвалят: " + ", ".join(h["review_pros"]))
    tradeoffs = [bad[k] for k in ranked[::-1] if bd[k] <= 0.35 and w[k] >= 0.1][:2]
    if h.get("rooms"):
        tradeoffs.append(f"{h['rooms']} номера: в одном номере до {h['max_guests']} гостей")
    cheapest = min(x["price_total_rub"] for x in all_hotels)
    if h["price_total_rub"] > cheapest * 1.15:
        tradeoffs.append(f"на {_rub(h['price_total_rub'] - cheapest)} дороже самого дешёвого подходящего")
    risks = []
    if not h["free_cancellation"]:
        risks.append("при отмене деньги не вернут")
    if h["reviews"] < 60:
        risks.append(f"мало отзывов ({h['reviews']}) — оценка может быть неточной")
    risks += [f"в отзывах жалуются: {c}" for c in h["review_cons"]]
    return {"why": why, "tradeoffs": tradeoffs, "risks": risks}


def explain_transport(o: dict, state: TripState) -> dict:
    why, tradeoffs, risks = [], [], []
    if o["breakdown"]["price"] >= 0.7: why.append("один из самых дешёвых")
    if o["breakdown"]["travel_time"] >= 0.7: why.append(f"быстро: {o['duration_h']} ч в пути")
    if o["stops"] == 0: why.append("без пересадок")
    if o["mode"] == "train": why.append("поезд: багаж без ограничений, можно с животными")
    if o["stops"]: tradeoffs.append("с пересадкой")
    if not o["baggage"]: tradeoffs.append("багаж оплачивается отдельно")
    if o.get("too_long"): tradeoffs.append(f"дольше желаемых {state.constraints.max_travel_hours:g} ч")
    dep_hour = int(o["departure"][11:13])
    if dep_hour < 6 or dep_hour >= 23: risks.append("ночной рейс")
    if o["stops"]: risks.append("короткая стыковка может сорваться при задержке")
    return {"why": why, "tradeoffs": tradeoffs, "risks": risks}


def explain_destination(d: dict) -> dict:
    risks = []
    if d["visa"] and d["visa"] not in ("не нужна",):
        risks.append(f"виза: {d['visa']}")
    return {"why": d.get("reasons", []), "tradeoffs": d.get("tradeoffs", []), "risks": risks}
