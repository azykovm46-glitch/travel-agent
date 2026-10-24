"""Политика уточнений и выбор следующего шага.

Принцип: спрашиваем только то, без чего нельзя искать (откуда, когда, кто едет),
не больше 2 вопросов и не больше одного раунда. Остальное закрываем
допущениями по умолчанию, которые пользователь видит и может поправить.
"""
from __future__ import annotations

from datetime import date

from agent.schemas import Question, QuickReply
from agent.state import TripState

REQUIRED = ["origin", "dates", "travelers"]
MAX_QUESTIONS = 2
MAX_ROUNDS = 1

MONTH_IN = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе",
            "сентябре", "октябре", "ноябре", "декабре"]


def missing_slots(state: TripState) -> list[str]:
    return [s for s in REQUIRED if s not in state.confirmed]


def build_questions(state: TripState, today: date | None = None) -> list[Question]:
    today = today or date.today()
    if state.clarify_rounds >= MAX_ROUNDS:
        return []
    qs: list[Question] = []
    for slot in missing_slots(state)[:MAX_QUESTIONS]:
        if slot == "origin":
            qs.append(Question(slot=slot, text="Откуда отправляетесь?", options=[
                QuickReply(label=c, text=f"из {t}") for c, t in
                [("Москва", "Москвы"), ("Санкт-Петербург", "Санкт-Петербурга"), ("Казань", "Казани"), ("Екатеринбург", "Екатеринбурга")]]))
        elif slot == "dates":
            m1 = today.month % 12 + 1
            m2 = m1 % 12 + 1
            qs.append(Question(slot=slot, text="Когда примерно и на сколько ночей?", options=[
                QuickReply(label=f"В {MONTH_IN[m1 - 1]}, 7 ночей", text=f"в {MONTH_IN[m1 - 1]} на 7 ночей"),
                QuickReply(label=f"В {MONTH_IN[m2 - 1]}, 10 ночей", text=f"в {MONTH_IN[m2 - 1]} на 10 ночей"),
                QuickReply(label="Выходные в следующем месяце", text=f"в {MONTH_IN[m1 - 1]} на 2 ночи")]))
        elif slot == "travelers":
            qs.append(Question(slot=slot, text="Кто едет?", options=[
                QuickReply(label="Один", text="один"), QuickReply(label="Вдвоём", text="вдвоём"),
                QuickReply(label="Вдвоём с ребёнком", text="вдвоём с ребёнком 6 лет"),
                QuickReply(label="4 взрослых", text="4 взрослых")]))
    return qs


def apply_defaults(state: TripState, today: date | None = None) -> None:
    """Если пользователь не ответил на уточнения — двигаемся с допущениями."""
    today = today or date.today()
    if "origin" not in state.confirmed and not state.origin_city:
        state.origin_city = "Москва"
        state.add_assumption("origin", "из Москвы", "город отправления не назван", "default")
    if "dates" not in state.confirmed and not state.dates.is_known:
        state.dates.month = today.month % 12 + 1
        state.add_assumption("month", MONTH_IN[state.dates.month - 1], "даты не названы — взяли следующий месяц", "default")
    if "travelers" not in state.confirmed:
        state.add_assumption("adults", f"{state.travelers.adults} взрослых" if state.travelers.adults > 1 else "1 взрослый",
                             "состав не назван", "default")


def next_step(state: TripState, intent: str) -> str:
    if state.stage == "booked":
        return "booked"
    if intent == "checkout" and state.plan:
        return "checkout"
    if intent == "select":
        return "select"
    if missing_slots(state) and state.clarify_rounds < MAX_ROUNDS:
        return "clarify"
    if not state.selection.destination_id:
        return "inspire"
    return "plan"
