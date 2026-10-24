"""Детерминированный скоринг вариантов.

Каждая функция возвращает варианты, отсортированные по score, и для каждого
разбор по критериям (breakdown, 0..1). Из breakdown строятся объяснения:
почему подходит, чем пожертвовали. LLM цифры не придумывает.
"""
from __future__ import annotations

from datetime import datetime

from agent.state import TripState

MEAL_RANK = {"RO": 0.0, "BB": 0.5, "HB": 0.75, "AI": 1.0}


def _norm_inverse(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 1.0
    return max(0.0, min(1.0, (hi - value) / (hi - lo)))


def hotel_filters(h: dict, state: TripState) -> list[str]:
    """Жёсткие причины отказа. Пустой список = отель проходит."""
    reasons = []
    t = state.travelers
    if h["max_guests"] < t.total:
        reasons.append(f"номер до {h['max_guests']} гостей, а вас {t.total}")
    if t.pets and not h["pets_allowed"]:
        reasons.append("нельзя с животными")
    if "free_cancellation" in state.must_have and not h["free_cancellation"]:
        reasons.append("нет бесплатной отмены")
    if "all_inclusive" in state.must_have and h["meals"] != "AI":
        reasons.append("не «всё включено»")
    if "breakfast" in state.must_have and h["meals"] == "RO":
        reasons.append("без завтрака")
    return reasons


def score_hotels(hotels: list[dict], state: TripState) -> list[dict]:
    w = state.weights.normalized()
    ok = [h for h in hotels if not hotel_filters(h, state)]
    if not ok:
        return []
    prices = [h["price_total_rub"] for h in ok]
    lo, hi = min(prices), max(prices)
    sea = "sea" in state.trip_type
    scored = []
    for h in ok:
        if sea and h.get("beach_m") is not None:
            location = _norm_inverse(h["beach_m"], 50, 1200)
        else:
            location = _norm_inverse(h["center_km"], 0.3, 9)
        meals = MEAL_RANK[h["meals"]]
        if state.travelers.has_kids:
            meals = min(1.0, meals + 0.15)
        bd = {
            "price": _norm_inverse(h["price_total_rub"], lo, hi),
            "travel_time": _norm_inverse(h["airport_km"], 5, 45),
            "location": location,
            "rating": max(0.0, min(1.0, (h["rating"] - 6.0) / 3.7)),
            "cancellation": 1.0 if h["free_cancellation"] else 0.0,
            "meals": meals,
        }
        bonus = 0.0
        if state.travelers.has_kids and (h["family_rooms"] or h["kids_club"]):
            bonus += 0.05
        score = sum(bd[k] * w[k] for k in bd) + bonus
        scored.append({**h, "score": round(score, 3), "breakdown": {k: round(v, 2) for k, v in bd.items()}})
    return sorted(scored, key=lambda x: x["score"], reverse=True)


def score_transport(options: list[dict], state: TripState) -> list[dict]:
    c = state.constraints
    ok = []
    for o in options:
        if c.direct_only and o["stops"]:
            continue
        if c.transport != "any" and o["mode"] != c.transport:
            continue
        ok.append(o)
    if not ok:
        ok = options          # не отдаём пустоту — объясним компромисс
    prices = [o["price_rub"] for o in ok]
    durs = [o["duration_h"] for o in ok]
    out = []
    for o in ok:
        dep_hour = datetime.fromisoformat(o["departure"]).hour
        night = dep_hour < 6 or dep_hour >= 23
        bd = {
            "price": _norm_inverse(o["price_rub"], min(prices), max(prices)),
            "travel_time": _norm_inverse(o["duration_h"], min(durs), max(durs)),
            "comfort": (0.0 if night and state.travelers.has_kids else 0.6 if night else 1.0)
                       - 0.3 * o["stops"] + (0.0 if o["baggage"] else -0.2),
        }
        too_long = c.max_travel_hours is not None and o["duration_h"] > c.max_travel_hours
        score = 0.45 * bd["price"] + 0.35 * bd["travel_time"] + 0.2 * bd["comfort"] - (0.3 if too_long else 0)
        out.append({**o, "score": round(score, 3), "breakdown": {k: round(v, 2) for k, v in bd.items()},
                    "too_long": too_long})
    return sorted(out, key=lambda x: x["score"], reverse=True)


def score_destinations(dests: list[dict], state: TripState) -> list[dict]:
    """Ранжирование направлений под запрос: тип отдыха, погода, время в пути, бюджет."""
    out = []
    budget = state.budget.total_rub
    for d in dests:
        reasons, minus = [], []
        s = 0.0
        if state.trip_type:
            match = len(set(state.trip_type) & set(d["tags"])) / len(set(state.trip_type))
            s += 0.35 * match
            if match:
                reasons.append("подходит под тип отдыха")
        else:
            s += 0.15
        if "sea" in state.trip_type:
            water = d["water_temp"] or 0
            warm = max(0.0, min(1.0, (water - 18) / 8))
            s += 0.25 * warm
            if water >= 24:
                reasons.append(f"вода около {water}°C")
            elif water < 20:
                s -= 0.4                      # «тёплое море» с холодной водой — почти отказ
                minus.append(f"вода холодная, около {water}°C — для купания не подходит")
            elif water < 23:
                minus.append(f"вода прохладная, около {water}°C")
        else:
            s += 0.25 * max(0.0, min(1.0, (d["air_temp"] - 5) / 20))
        hours = d["flight_hours"]
        if d.get("train_hours") and state.constraints.transport == "train":
            hours = d["train_hours"]
        limit = state.constraints.max_travel_hours
        if limit and hours > limit:
            s -= 0.3
            minus.append(f"дорога {hours} ч — дольше желаемых {limit:g} ч")
        else:
            s += 0.2 * max(0.0, 1 - hours / 10)
            if hours <= 4:
                reasons.append(f"лететь около {hours} ч")
        if budget:
            ratio = d["est_total_rub"] / budget
            s += 0.2 * max(0.0, min(1.0, 1.4 - ratio))
            if ratio > 1.05:
                minus.append("ориентировочно дороже бюджета")
            elif ratio < 0.85:
                reasons.append("укладывается в бюджет с запасом")
        elif state.budget.level == "low":
            s += 0.2 * max(0.0, 1 - d["est_night_rub"] / 15000)
        out.append({**d, "score": round(s, 3), "reasons": reasons, "tradeoffs": minus})
    return sorted(out, key=lambda x: x["score"], reverse=True)
