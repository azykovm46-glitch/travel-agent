"""Тесты ядра агента. Работают без LLM и без сети.

    python -m unittest discover -s tests -v      # или: pytest -q
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OFFLINE"] = "1"

from agent import TravelAgent  # noqa: E402
from agent.state import TripState  # noqa: E402
from agent.understand import apply_patch, infer_hidden, rule_patch  # noqa: E402
from tools.validator import validate_plan  # noqa: E402

TODAY = date(2026, 10, 25)
DEMO = "хочу к тёплому морю в ноябре, недорого, с ребёнком 5 лет, без долгих перелётов, из Москвы"


def new_agent() -> TravelAgent:
    return TravelAgent(today=TODAY)


class UnderstandTest(unittest.TestCase):
    def test_demo_query_extracts_explicit_and_hidden(self):
        s = TripState()
        p = rule_patch(DEMO, s)
        self.assertEqual(p["origin_city"], "Москва")
        self.assertEqual(p["month"], 11)
        self.assertEqual(p["children_ages"], [5])
        self.assertEqual(p["budget_level"], "low")
        self.assertEqual(p["max_travel_hours"], 4)
        self.assertIn("sea", p["trip_type"])
        apply_patch(s, p)
        infer_hidden(s)
        self.assertEqual(s.travelers.adults, 2)                       # скрытое: двое взрослых
        self.assertTrue(any(a.field == "kids" for a in s.assumptions))  # допущение видно

    def test_shift_is_not_nights(self):
        p = rule_patch("а если на 2 дня позже?", TripState())
        self.assertEqual(p.get("shift_days"), 2)
        self.assertNotIn("nights", p)

    def test_nights_and_pets(self):
        p = rule_patch("на 10 ночей с собакой", TripState())
        self.assertEqual(p["nights"], 10)
        self.assertEqual(p["pets"], 1)

    def test_budget_number(self):
        self.assertEqual(rule_patch("бюджет 150 тысяч", TripState())["budget_total_rub"], 150_000)
        self.assertEqual(rule_patch("до 80к на двоих", TripState())["budget_total_rub"], 80_000)


class FlowTest(unittest.TestCase):
    def test_vague_query_asks_at_most_two_questions_once(self):
        a = new_agent()
        r = a.handle(message="хочу куда-нибудь отдохнуть")
        self.assertEqual(r.stage, "clarify")
        self.assertLessEqual(len(r.questions), 2)
        r2 = a.handle(r.session_id, message="не знаю")
        self.assertNotEqual(r2.stage, "clarify")                     # второго раунда нет

    def test_full_path_to_booking(self):
        a = new_agent()
        r = a.handle(message=DEMO)
        self.assertEqual(r.stage, "inspire")
        self.assertEqual(len([c for c in r.cards if c.type == "destination"]), 3)
        top = r.cards[0].data
        self.assertGreaterEqual(top["water_temp"], 20)               # «тёплое море» — не Калининград
        r = a.handle(r.session_id, action={"type": "select_destination", "id": top["id"]})
        self.assertEqual(r.stage, "plan")
        plan = r.state.plan
        for key in ("outbound", "return", "hotel", "transfer_in", "transfer_out"):
            self.assertIn(key, plan)
        self.assertTrue(plan["hotel"]["explanation"]["why"])         # объяснение есть
        r = a.handle(r.session_id, action={"type": "checkout"})
        self.assertEqual(r.stage, "booked")
        self.assertTrue(r.state.order["order_id"].startswith("OST-"))

    def test_replan_keeps_destination_and_user_hotel(self):
        a = new_agent()
        r = a.handle(message="Сочи на неделю в ноябре вдвоём из Москвы")
        self.assertEqual(r.stage, "plan")
        alt = r.state.candidates["hotels_top"][1]["id"]
        r = a.handle(r.session_id, action={"type": "select_hotel", "id": alt})
        self.assertEqual(r.state.plan["hotel"]["id"], alt)
        r = a.handle(r.session_id, message="а если на 2 дня позже?")
        self.assertEqual(r.state.selection.destination_id, "sochi")
        self.assertEqual(r.state.plan["hotel"]["id"], alt)           # выбор пользователя сохранён
        self.assertTrue(any(d["field"] == "dates" for d in r.diff))  # показан дифф
        self.assertTrue(any(s.step == "replan" for s in r.trace))

    def test_pet_filters_hotels_and_transfer(self):
        a = new_agent()
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы с собакой")
        plan = r.state.plan
        self.assertTrue(plan["hotel"]["pets_allowed"])
        self.assertEqual(plan["transfer_in"]["kind"], "taxi")

    def test_weights_change_reranks_without_new_search(self):
        a = new_agent()
        r = a.handle(message="Анталья в ноябре вдвоём из Москвы на 7 ночей")
        r = a.handle(r.session_id, action={"type": "set_weights", "weights": {"price": 0.05, "rating": 0.6}})
        replan = [s for s in r.trace if s.step == "replan"]
        self.assertTrue(replan and "переищу: ничего" in replan[0].detail)


class FakeLLM:
    """Подмена LLM: проверяем, что патч модели перекрывает правила и текст модели доходит до ответа."""
    enabled = True
    model = "fake"

    def extract_patch(self, message, state_summary, today):
        return {"intent": "plan", "origin_city": "Москва", "trip_type": ["sea"], "month": 11,
                "adults": 2, "budget_level": "low"}

    def write(self, facts):
        return f"[LLM] этап {facts['stage']}"


class LLMPathTest(unittest.TestCase):
    def test_llm_patch_understands_english(self):
        a = TravelAgent(llm=FakeLLM(), today=TODAY)
        r = a.handle(message="beach trip in november for two from Moscow, cheap")
        self.assertEqual(r.stage, "inspire")                          # без лишних вопросов
        self.assertTrue(r.message.startswith("[LLM]"))
        self.assertIn("LLM", r.trace[0].detail)


class ValidatorTest(unittest.TestCase):
    def test_early_arrival_and_date_mismatch(self):
        plan = {
            "check_in": "2026-11-14", "check_out": "2026-11-21", "total_rub": 100,
            "outbound": {"arrival": "2026-11-14T08:00", "mode": "plane"},
            "return": {"departure": "2026-11-22T10:00", "mode": "plane"},
            "hotel": {"check_in": "15:00", "check_out": "12:00", "max_guests": 2, "pets_allowed": False,
                      "early_checkin": True, "free_cancellation": True},
            "transfer_in": {"minutes": 30}, "transfer_out": {"minutes": 30},
        }
        codes = {i["code"]: i["level"] for i in validate_plan(plan, 2, 0, None)}
        self.assertEqual(codes["early_arrival"], "warning")
        self.assertEqual(codes["dates_mismatch"], "error")


if __name__ == "__main__":
    unittest.main()
