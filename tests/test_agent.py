"""Тесты ядра агента. Работают без LLM и без сети.

    python -m unittest discover -s tests -v      # или: pytest -q
"""
from __future__ import annotations

import functools
import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OFFLINE"] = "1"

import logging  # noqa: E402
import tempfile  # noqa: E402
from types import SimpleNamespace  # noqa: E402

logging.disable(logging.WARNING)      # предупреждения о сбоях LLM в тестах ожидаемы

from agent import TravelAgent  # noqa: E402
from agent.explain import unsupported_numbers  # noqa: E402
from agent.llm import LLM, clean_patch  # noqa: E402
from agent.memory import ProfileStore, SessionStore  # noqa: E402
from agent.state import TripState  # noqa: E402
from agent.understand import apply_patch, infer_hidden, merge_patches, rule_patch  # noqa: E402
from agent.places import lookup_place, region_of  # noqa: E402
from tools import get_backend, mock_backend  # noqa: E402
from unittest import mock  # noqa: E402
from tools.validator import validate_plan  # noqa: E402

TODAY = date(2026, 10, 25)
DEMO = "хочу к тёплому морю в ноябре, недорого, с ребёнком 5 лет, без долгих перелётов, из Москвы"


def on_mock(test):
    """Тесту нужны города мок-бэкенда (Владивосток, горнолыжные курорты) — гоняем его на моке при любом
    TOOLS_BACKEND. Остальные тесты проверяют логику агента и должны проходить на любом бэкенде."""
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        with mock.patch.dict(os.environ, {"TOOLS_BACKEND": "tools.mock_backend"}):
            get_backend.cache_clear()
            try:
                return test(*args, **kwargs)
            finally:
                get_backend.cache_clear()
    return wrapper


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

    def test_group_size_in_words(self):
        self.assertEqual(rule_patch("нас трое, из Москвы", TripState())["adults"], 3)
        self.assertEqual(rule_patch("нас четверо", TripState())["adults"], 4)
        self.assertEqual(rule_patch("едем нас 5 человек", TripState())["adults"], 5)

    def test_sparse_llm_patch_keeps_rule_fields(self):
        q = "с 28 декабря по 3 января в горы на лыжах, нас трое, из Екатеринбурга, бюджет 200к"
        rules = rule_patch(q, TripState(), TODAY)
        # так ответила реальная модель: почти пусто, и порядок типов другой
        merged = merge_patches(rules, {"intent": "plan", "trip_type": ["ski"]})
        for k in ("origin_city", "start", "end", "month", "budget_total_rub", "adults", "destination_hint"):
            self.assertEqual(merged[k], rules[k], k)
        self.assertEqual(sorted(merged["trip_type"]), ["nature", "ski"])   # списки объединяем, а не затираем

    def test_llm_false_or_zero_does_not_erase_rules(self):
        merged = merge_patches({"intent": "plan", "direct_only": True, "pets": 1},
                               {"intent": "plan", "direct_only": False, "pets": 0, "adults": 2})
        self.assertTrue(merged["direct_only"])
        self.assertEqual(merged["pets"], 1)
        self.assertEqual(merged["adults"], 2)                           # непустые значения LLM доходят

    def test_llm_default_single_adult_does_not_hide_assumption(self):
        # реальный ответ модели на демо-запрос: adults 1, хотя взрослых не называли
        llm = {"intent": "plan", "origin_city": "Москва", "month": 11, "adults": 1, "children_ages": [5]}
        s = TripState()
        apply_patch(s, merge_patches(rule_patch(DEMO, s), llm))
        infer_hidden(s)
        self.assertEqual(s.travelers.adults, 2)
        self.assertTrue(any(a.field == "adults" for a in s.assumptions))
        solo = merge_patches(rule_patch("одна с ребёнком 5 лет", TripState()), llm)
        self.assertEqual(solo["adults"], 1)                              # сказано явно — верим

    def test_llm_other_intent_does_not_drop_travel_request(self):
        rules = rule_patch("в горы на лыжах из Москвы в декабре", TripState())
        self.assertEqual(merge_patches(rules, {"intent": "other"})["intent"], "plan")
        self.assertEqual(merge_patches({"intent": "plan"}, {"intent": "other"})["intent"], "other")

    def test_known_places_outside_catalog(self):
        self.assertEqual(lookup_place("хочу на пхукет")["country"], "Таиланд")
        self.assertEqual(lookup_place("стамбул в ноябре")["name"], "Стамбул")
        self.assertIn("ski", lookup_place("шерегеш")["tags"])
        self.assertIsNone(lookup_place("атлантида"))
        self.assertEqual(region_of("Китай"), region_of("Таиланд"))
        self.assertNotEqual(region_of("ОАЭ"), region_of("Таиланд"))

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

    def test_several_children_with_ages(self):
        p = rule_patch("на море с тремя детьми 3, 8 и 12 лет", TripState(), TODAY)
        self.assertEqual(p["children_ages"], [3, 8, 12])
        p = rule_patch("египет вдвоём с двумя детьми 4 и 9 лет", TripState(), TODAY)
        self.assertEqual(p["children_ages"], [4, 9])
        p = rule_patch("трое детей, без возраста", TripState(), TODAY)
        self.assertEqual(len(p["children_ages"]), 3)

    def test_exact_dates(self):
        p = rule_patch("в Сочи 5 марта на 3 ночи", TripState(), TODAY)
        self.assertEqual(p["start"], "2027-03-05")                  # март уже прошёл — следующий год
        p = rule_patch("с 14 по 21 ноября", TripState(), TODAY)
        self.assertEqual((p["start"], p["end"]), ("2026-11-14", "2026-11-21"))
        p = rule_patch("с 28 декабря по 3 января", TripState(), TODAY)
        self.assertEqual((p["start"], p["end"]), ("2026-12-28", "2027-01-03"))

    def test_declined_destination_names(self):
        self.assertEqual(rule_patch("хочу в Анталью", TripState(), TODAY)["destination_name"], "Анталья")
        self.assertEqual(rule_patch("летим в Турцию", TripState(), TODAY)["destination_name"], "Анталья")

    def test_unknown_place_is_kept_for_honest_answer(self):
        p = rule_patch("хочу в Атлантиду", TripState(), TODAY)
        self.assertEqual(p["destination_name"], "Атлантиду")
        self.assertEqual(rule_patch("из Тмутаракани в Сочи", TripState(), TODAY)["unknown_origin"], "Тмутаракани")

    def test_typos_without_llm(self):
        p = rule_patch("хачу на мори в дикабре с жиной недорага", TripState(), TODAY)
        self.assertEqual((p["month"], p["adults"], p["budget_level"]), (12, 2, "low"))
        self.assertIn("sea", p["trip_type"])
        self.assertIn("дикабре→декабре", p["typo_fixes"])
        p = rule_patch("из масквы в анталию", TripState(), TODAY)
        self.assertEqual((p["origin_city"], p["destination_name"]), ("Москва", "Анталья"))

    def test_typo_fix_does_not_invent(self):
        p = rule_patch("хочу в горы на лыжах, вдвоём", TripState(), TODAY)
        self.assertNotIn("sea", p.get("trip_type", []))                # «горы» не стали «морем»
        self.assertNotIn("typo_fixes", p)

    def test_english_without_llm(self):
        p = rule_patch("beach trip in november for two from Moscow, cheap", TripState(), TODAY)
        self.assertEqual((p["origin_city"], p["month"], p["adults"], p["budget_level"]), ("Москва", 11, 2, "low"))
        self.assertIn("sea", p["trip_type"])
        p = rule_patch("Antalya for 7 nights in December with a 5 year old kid from Kazan", TripState(), TODAY)
        self.assertEqual((p["destination_name"], p["nights"], p["children_ages"], p["origin_city"], p["month"]),
                         ("Анталья", 7, [5], "Казань", 12))

    def test_closer_to_sea_is_weight_not_search(self):
        p = rule_patch("хочу отель ближе к морю", TripState(), TODAY)
        self.assertEqual(p["prefer"], "location")


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
        self.assertTrue(r.state.order["order_id"])                     # префикс у бэкендов разный: OST-, OD-

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

    def test_closer_to_sea_reranks_hotels(self):
        a = new_agent()
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы")       # море не названо — до сих пор мерили до центра
        before = r.state.plan["hotel"]["beach_m"]
        loc = r.state.weights.location
        r = a.handle(r.session_id, message="хочу отель ближе к морю")
        self.assertGreater(r.state.weights.location, loc)
        self.assertLess(r.state.plan["hotel"]["beach_m"], before)    # «ближе» — ближе, чем было
        replan = next(s.detail for s in r.trace if s.step == "replan")
        self.assertIn("переищу: ничего", replan)                      # только переранжирование

    @on_mock
    def test_ski_does_not_offer_beach(self):
        a = new_agent()
        r = a.handle(message="в горы на лыжах в феврале вдвоём из Москвы")
        top = [c.data for c in r.cards if c.type == "destination"]
        self.assertIn("ski", top[0]["tags"])
        self.assertNotIn("phuket", [d["id"] for d in top])

    def test_ski_plan_has_no_stitch_errors(self):
        a = new_agent()
        r = a.handle(message="в горы на лыжах в феврале вдвоём из Москвы")
        r = a.handle(r.session_id, action=r.quick_replies[0].action)  # у курортов долгий трансфер
        self.assertFalse([i for i in r.state.plan["issues"] if i["level"] == "error"])

    def test_big_family_gets_several_rooms(self):
        a = new_agent()
        r = a.handle(message="Шарм в ноябре вдвоём с тремя детьми 3, 8 и 12 лет из Москвы")
        hotel = r.state.plan["hotel"]
        self.assertGreaterEqual(hotel["max_guests"] * hotel.get("rooms", 1), 5)
        self.assertTrue(any(q.action == {"type": "checkout"} for q in r.quick_replies))

    def test_exact_date_reaches_plan(self):
        a = new_agent()
        r = a.handle(message="в Сочи вдвоём из Москвы 5 марта на 3 ночи")
        self.assertEqual((r.state.plan["check_in"], r.state.plan["check_out"]), ("2027-03-05", "2027-03-08"))

    def test_unknown_destination_is_named(self):
        a = new_agent()
        r = a.handle(message="хочу в Атлантиду")
        self.assertIn("Атлантид", r.message)
        self.assertNotIn("..", r.message)

    def test_unknown_origin_is_named(self):
        a = new_agent()
        r = a.handle(message="из Тмутаракани в Сочи в ноябре вдвоём")
        self.assertIn("Тмутаракан", r.message)

    @on_mock
    def test_regional_origin_known(self):
        a = new_agent()
        r = a.handle(message="из Владивостока в Анталью в ноябре вдвоём")
        self.assertEqual(r.state.origin_city, "Владивосток")
        self.assertEqual(r.state.selection.destination_id, "antalya")
        self.assertEqual(r.stage, "plan")

    def test_destination_change_shows_in_diff(self):
        a = new_agent()
        r = a.handle(message="Сочи вдвоём из Москвы в ноябре")
        r = a.handle(r.session_id, message="а в Шарм?")
        self.assertEqual(r.diff[0]["field"], "destination")

    def test_direct_only_reranks_flights_without_new_search(self):
        a = new_agent()
        r = a.handle(message="Анталья в ноябре вдвоём из Москвы")
        r = a.handle(r.session_id, message="а можно без пересадок?")
        self.assertEqual((r.state.plan["outbound"]["stops"], r.state.plan["return"]["stops"]), (0, 0))
        self.assertIn("переищу: ничего", next(s.detail for s in r.trace if s.step == "replan"))

    def test_other_hotel_picks_next_and_keeps_flights(self):
        a = new_agent()
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы")
        first, out = r.state.plan["hotel"]["id"], r.state.plan["outbound"]["id"]
        r = a.handle(r.session_id, message="давай другой отель")
        self.assertNotEqual(r.state.plan["hotel"]["id"], first)
        self.assertEqual(r.state.plan["outbound"]["id"], out)
        self.assertEqual([d["field"] for d in r.diff if d["field"] == "hotel"], ["hotel"])

    def test_dog_joins_after_plan(self):
        a = new_agent()
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы")
        r = a.handle(r.session_id, message="с нами едет собака")
        self.assertTrue(r.state.plan["hotel"]["pets_allowed"])
        self.assertEqual(r.state.plan["transfer_in"]["kind"], "taxi")
        self.assertTrue(any(s.step == "replan" for s in r.trace))

    def test_after_booking_reminders_and_early_checkin(self):
        a = new_agent()
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы")
        r = a.handle(r.session_id, action={"type": "checkout"})
        order = r.state.order
        times = [x["at"] for x in order["reminders"]]
        self.assertEqual(times, sorted(times))
        self.assertTrue(any("онлайн-регистрация" in x["text"].lower() for x in order["reminders"]))
        r = a.handle(r.session_id, message="можно заселиться пораньше?")
        self.assertEqual(r.stage, "booked")
        svc = r.state.order["services"]
        self.assertEqual([s["kind"] for s in svc], ["early_checkin"])
        self.assertIn(svc[0]["status"], ("confirmed", "on_request", "declined", "requested"))
        self.assertNotIn("Запросить ранний заезд", [q.label for q in r.quick_replies])
        r = a.handle(r.session_id, message="напомни, что когда")
        self.assertTrue(r.message.startswith("Напомню:"))
        self.assertEqual(r.state.order["order_id"], order["order_id"])      # бронь не трогали

    def test_closer_to_sea_respects_other_filters(self):
        a = new_agent()
        r = a.handle(message="Сочи на неделю в ноябре вдвоём из Москвы с собакой")
        before = r.state.plan["hotel"]
        r = a.handle(r.session_id, message="хочу отель ближе к морю")
        self.assertIsNotNone(r.state.plan)                             # не «не нашёл отелей»
        self.assertTrue(r.state.plan["hotel"]["pets_allowed"])
        self.assertLessEqual(r.state.plan["hotel"]["beach_m"], before["beach_m"])

    def test_no_hotels_means_nothing_to_book(self):
        a = new_agent()
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы")
        state = r.state
        state.constraints.max_beach_m = 1                               # условие, которому не удовлетворить
        state.candidates.pop("hotels")
        r = a.handle(r.session_id, message="с бесплатной отменой")
        self.assertIsNone(r.state.plan)
        r = a.handle(r.session_id, action={"type": "checkout"})
        self.assertNotEqual(r.stage, "booked")                          # старый план молча не бронируем
        r = a.handle(r.session_id, action={"type": "change_destination"})
        self.assertEqual(r.stage, "inspire")

    @on_mock
    def test_night_arrival_keeps_check_in_day(self):
        a = new_agent()
        r = a.handle(message="из Владивостока в Шарм с 29 декабря по 4 января вдвоём")
        plan = r.state.plan
        self.assertEqual(plan["check_in"], "2026-12-29")
        self.assertLessEqual(plan["outbound"]["arrival"], "2026-12-30T04:30")

    def test_offtopic_is_gently_redirected(self):
        a = new_agent()
        r = a.handle(message="какая погода на марсе?")
        self.assertTrue(r.message.startswith("Я помогаю спланировать поездку"))
        r = a.handle(r.session_id, message="не знаю")                  # второй ход — без нотаций
        self.assertNotIn("Я помогаю", r.message)

    @on_mock
    def test_ski_in_season_needs_no_questions(self):
        a = new_agent()
        r = a.handle(message="с 28 декабря по 3 января в горы на лыжах, нас трое, из Екатеринбурга, бюджет 200к")
        self.assertEqual(r.stage, "inspire")
        top = r.cards[0].data
        self.assertIn("ski", top["tags"])
        self.assertTrue(top["ski_season"])
        self.assertNotIn("сезон", r.message.split(".")[0])            # без лишних оговорок

    @on_mock
    def test_ski_out_of_season_is_honest_and_offers_nearest(self):
        a = new_agent()
        r = a.handle(message="хочу покататься на лыжах в июле из Москвы вдвоем")
        self.assertIn("в июле", r.message)
        self.assertIn("не сезон", r.message)
        self.assertIn("с ноября", r.message)                           # Шерегеш открывается раньше всех
        move = [q for q in r.quick_replies if q.action.get("patch", {}).get("month") == 11]
        self.assertTrue(move)
        r = a.handle(r.session_id, action=move[0].action)
        self.assertTrue(r.cards[0].data["ski_season"])
        self.assertNotIn("не сезон", r.message)

    @on_mock
    def test_all_inclusive_unavailable_offers_breakfast_and_books(self):
        a = new_agent()
        r = a.handle(message="всё включено в Казань на неделю в июне вдвоём из Москвы")
        self.assertIsNone(r.state.plan)                                # в Казани нет «всё включено»
        relax = r.quick_replies[0]
        self.assertEqual(relax.label, "Показать с завтраком вместо «всё включено»")
        r = a.handle(r.session_id, action=relax.action)
        self.assertNotIn("all_inclusive", r.state.must_have)
        self.assertIn("breakfast", r.state.must_have)
        self.assertEqual(r.state.selection.destination_id, "kazan")    # направление не сбросилось
        self.assertNotEqual(r.state.plan["hotel"]["meals"], "RO")
        r = a.handle(r.session_id, action={"type": "checkout"})
        self.assertEqual(r.stage, "booked")

    def test_relax_offers_only_conditions_that_block_hotels(self):
        search = get_backend().search_hotels
        no_cancel = lambda *a: [{**h, "free_cancellation": False} for h in search(*a)]  # noqa: E731
        with mock.patch.object(get_backend(), "search_hotels", no_cancel):
            a = new_agent()
            r = a.handle(message="в Сочи на неделю в ноябре вдвоём из Москвы, с завтраком и бесплатной отменой")
            self.assertIsNone(r.state.plan)
            labels = [q.label for q in r.quick_replies]
            self.assertIn("Показать без бесплатной отмены", labels)
            self.assertNotIn("Показать без завтрака", labels)         # завтрак отели не отсеивает
            r = a.handle(r.session_id, action=r.quick_replies[0].action)
        self.assertEqual(r.state.must_have, ["breakfast"])
        self.assertIsNotNone(r.state.plan)

    @on_mock
    def test_no_ski_resorts_in_catalog_is_said_honestly(self):
        no_ski = [d for d in mock_backend.DESTINATIONS if "ski" not in d["tags"]]
        with mock.patch.object(mock_backend, "DESTINATIONS", no_ski):
            r = new_agent().handle(message="на лыжах в январе из Москвы вдвоем")
        self.assertIn("горнолыжных курортов", r.message.lower())
        self.assertIn("nature", r.cards[0].data["tags"])               # ближайшее по духу — горы и природа

    def test_luxury_with_tiny_budget_is_called_out(self):
        a = new_agent()
        r = a.handle(message="люкс за 30 тысяч в Дубай вдвоём из Москвы в декабре")
        self.assertIn("люкс", r.message.lower())


class ReplanTest(unittest.TestCase):
    """Каждая фраза пересборки: что переискано, а что взято из кэша, видно в trace."""
    BASE = "Анталья в ноябре вдвоём из Москвы на неделю"

    def replan(self, phrase: str):
        a = new_agent()
        r = a.handle(message=self.BASE)
        self.assertEqual(r.stage, "plan")
        before = r.state.model_copy(deep=True)
        r = a.handle(r.session_id, message=phrase)
        self.assertEqual(r.stage, "plan")
        trace = {}
        for t in r.trace:
            trace.setdefault(t.step, t.detail)
        return before, r, trace

    def test_pet_researches_hotels_and_transfers(self):
        before, r, tr = self.replan("с нами едет собака")
        self.assertIn("переищу: hotels, outbound, return, transfers", tr["replan"])
        self.assertIn("новый поиск", tr["search_hotels"])
        self.assertTrue(r.state.plan["hotel"]["pets_allowed"])

    def test_direct_only_reuses_search(self):
        before, r, tr = self.replan("без пересадок")
        self.assertIn("переищу: ничего", tr["replan"])
        self.assertIn("из кэша", tr["search_transport"])
        self.assertIn("из кэша", tr["search_hotels"])
        self.assertEqual(r.state.plan["outbound"]["stops"], 0)
        self.assertEqual(r.state.plan["hotel"]["id"], before.plan["hotel"]["id"])     # отель сохранён

    def test_closer_to_sea_changes_weights_not_search(self):
        before, r, tr = self.replan("хочу отель ближе к морю")
        self.assertIn("weights", tr["replan"])
        self.assertIn("переищу: ничего", tr["replan"])
        self.assertIn("из кэша", tr["search_hotels"])
        self.assertGreater(r.state.weights.location, before.weights.location)
        self.assertLess(r.state.plan["hotel"]["beach_m"], before.plan["hotel"]["beach_m"])
        self.assertEqual(r.state.plan["outbound"]["id"], before.plan["outbound"]["id"])  # рейсы не тронуты

    def test_other_hotel_keeps_flights(self):
        before, r, tr = self.replan("давай другой отель")
        self.assertIn("сохранено: outbound, return, hotels", tr["replan"])
        self.assertIn("из кэша", tr["search_hotels"])
        self.assertNotEqual(r.state.plan["hotel"]["id"], before.plan["hotel"]["id"])
        self.assertEqual(r.state.plan["outbound"]["id"], before.plan["outbound"]["id"])

    def test_other_destination_researches_everything_but_keeps_dates(self):
        before, r, tr = self.replan("а в Шарм?")
        self.assertIn("переищу: hotels, outbound, return, transfers", tr["replan"])
        self.assertIn("новый поиск", tr["search_hotels"])
        self.assertEqual(r.state.selection.destination_id, "sharm")
        self.assertEqual(r.state.plan["check_in"], before.plan["check_in"])

    def test_shift_dates_researches_transport_and_hotels(self):
        before, r, tr = self.replan("на 2 дня позже")
        self.assertIn("dates", tr["replan"])
        self.assertIn("новый поиск", tr["search_transport"])
        self.assertIn("новый поиск", tr["search_hotels"])
        self.assertEqual(date.fromisoformat(r.state.plan["check_in"]),
                         date.fromisoformat(before.plan["check_in"]) + timedelta(days=2))

    def test_smaller_budget_reranks_cached_options(self):
        before, r, tr = self.replan("бюджет меньше")
        self.assertIn("переищу: ничего", tr["replan"])
        self.assertIn("из кэша", tr["search_hotels"])
        self.assertLess(r.state.budget.total_rub, before.plan["total_rub"])           # от суммы текущего плана
        self.assertLessEqual(r.state.plan["total_rub"], before.plan["total_rub"])


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.store = ProfileStore(Path(tempfile.mkdtemp()) / "profiles.sqlite3")

    def book(self, user_id, text, today=TODAY):
        a = TravelAgent(today=today, memory=self.store)
        r = a.handle(message=text, user_id=user_id)
        return a.handle(r.session_id, action={"type": "checkout"})

    def test_profile_saved_after_booking_and_applied_next_time(self):
        self.book("u1", "Анталья в ноябре вдвоём с ребёнком 5 лет из Казани с собакой, недорого")
        a = TravelAgent(today=date(2027, 6, 1), memory=self.store)          # через полгода, новый год
        r = a.handle(message="хочу на море в июле", user_id="u1")
        s = r.state
        self.assertNotEqual(r.stage, "clarify")
        self.assertEqual((s.origin_city, s.travelers.adults, s.travelers.children_ages, s.travelers.pets),
                         ("Казань", 2, [6], 1))                                 # ребёнок подрос
        self.assertEqual(s.budget.level, "low")
        self.assertTrue({"profile_origin", "profile_travelers"} <= {x.field for x in s.assumptions if x.source == "profile"})
        self.assertTrue(any(t.step == "profile" for t in r.trace))

    def test_user_words_override_profile(self):
        self.book("u2", "Сочи в ноябре вдвоём из Казани")
        a = TravelAgent(today=TODAY, memory=self.store)
        r = a.handle(message="на море в декабре из Москвы", user_id="u2")
        self.assertEqual(r.state.origin_city, "Москва")
        self.assertNotIn("profile_origin", [x.field for x in r.state.assumptions])

    def test_removing_profile_assumption_reverts_value(self):
        self.book("u3", "Сочи в ноябре вдвоём из Казани")
        a = TravelAgent(today=TODAY, memory=self.store)
        r = a.handle(message="на море в декабре", user_id="u3")
        r = a.handle(r.session_id, action={"type": "remove_assumption", "field": "profile_origin"})
        self.assertNotEqual(r.state.origin_city, "Казань")
        self.assertNotIn("profile_origin", [x.field for x in r.state.assumptions])

    def test_other_user_and_no_memory_get_nothing(self):
        self.book("u4", "Сочи в ноябре вдвоём из Казани")
        r = TravelAgent(today=TODAY, memory=self.store).handle(message="на море в декабре", user_id="someone-else")
        self.assertEqual(r.stage, "clarify")
        r = TravelAgent(today=TODAY).handle(message="на море в декабре", user_id="u4")
        self.assertEqual(r.stage, "clarify")


    def test_profile_written_only_after_booking(self):
        a = TravelAgent(today=TODAY, memory=self.store)
        r = a.handle(message="Сочи в ноябре вдвоём из Казани", user_id="u5")
        self.assertEqual(r.stage, "plan")
        self.assertIsNone(self.store.get("u5"))                          # план без брони — не запоминаем
        a.handle(r.session_id, action={"type": "checkout"})
        self.assertEqual(self.store.get("u5")["origin_city"], "Казань")

    def test_without_user_id_memory_is_not_touched(self):
        self.book("u6", "Сочи в ноябре вдвоём из Казани")
        before = self._profiles()
        a = TravelAgent(today=TODAY, memory=self.store)
        r = a.handle(message="Анталья в декабре втроём из Москвы")       # user_id не передан
        a.handle(r.session_id, action={"type": "checkout"})
        self.assertEqual(self._profiles(), before)                        # ничего не записали
        r = a.handle(message="на море в декабре")
        self.assertEqual(r.stage, "clarify")                              # и ничего не подставили
        self.assertFalse([x for x in r.state.assumptions if x.source == "profile"])

    def _profiles(self) -> list:
        import sqlite3
        conn = sqlite3.connect(self.store.path)
        try:
            return conn.execute("SELECT user_id, data FROM profiles ORDER BY user_id").fetchall()
        finally:
            conn.close()


class SessionPersistenceTest(unittest.TestCase):
    def test_conversation_survives_server_restart(self):
        path = Path(tempfile.mkdtemp()) / "agent.sqlite3"
        a = TravelAgent(today=TODAY, sessions=SessionStore(path))
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы")
        sid, hotel = r.session_id, r.state.plan["hotel"]["id"]
        b = TravelAgent(today=TODAY, sessions=SessionStore(path))         # «перезапуск»: новый процесс, пустой кэш
        restored = b.sessions.get(sid)
        self.assertEqual(restored.plan["hotel"]["id"], hotel)
        self.assertIn("hotels", restored.candidates)                     # кэш поиска тоже сохранён
        r = b.handle(sid, message="давай другой отель")
        self.assertNotEqual(r.state.plan["hotel"]["id"], hotel)
        self.assertEqual([d["field"] for d in r.diff if d["field"] == "hotel"], ["hotel"])
        r = b.handle(sid, action={"type": "checkout"})
        self.assertEqual(r.stage, "booked")

    def test_without_path_nothing_is_written(self):
        store = SessionStore()
        TravelAgent(today=TODAY, sessions=store).handle(message="Сочи в ноябре вдвоём из Москвы")
        self.assertIsNone(store.path)


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


class OffTopicLLM(FakeLLM):
    """Модель, которая распознаёт офтоп: на вопрос не о поездке — intent other."""

    def extract_patch(self, message, state_summary, today):
        return {"intent": "other"} if "марс" in message else super().extract_patch(message, state_summary, today)


class OffTopicTest(unittest.TestCase):
    def test_offtopic_mid_conversation_returns_to_trip(self):
        a = TravelAgent(llm=OffTopicLLM(), today=TODAY)
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы")
        plan = r.state.plan
        r = a.handle(r.session_id, message="а какая погода на марсе?")
        self.assertIn("С этим вопросом не подскажу", r.message)
        self.assertIn("поездк", r.message)
        self.assertEqual(r.state.plan["total_rub"], plan["total_rub"])  # план не тронут
        self.assertFalse(r.diff)


class ScriptedLLM(FakeLLM):
    """Разбор задаём в тесте; пишет коротко и без чисел — как модель, которая опускает оговорки."""

    def __init__(self, patch):
        self.patch = patch

    def extract_patch(self, message, state_summary, today):
        return dict(self.patch)

    def write(self, facts):
        return "Готово, смотрите план."


class LLMHonestyTest(unittest.TestCase):
    def test_llm_cannot_invent_origin_city(self):
        llm = ScriptedLLM({"intent": "plan", "origin_city": "Тмутаракань", "destination_name": "Сочи",
                           "month": 11, "adults": 2})
        r = TravelAgent(llm=llm, today=TODAY).handle(message="из Тмутаракани в Сочи в ноябре вдвоём")
        self.assertIn("Тмутаракан", r.message)                         # честно сказали
        self.assertNotEqual(r.state.origin_city, "Тмутаракань")         # и не считаем от выдуманного города

    def test_luxury_conflict_survives_llm_text(self):
        llm = ScriptedLLM({"intent": "plan", "origin_city": "Москва", "destination_name": "Дубай", "month": 12,
                           "adults": 2, "budget_total_rub": 30000, "budget_level": "high"})
        r = TravelAgent(llm=llm, today=TODAY).handle(message="люкс за 30 тысяч в Дубай вдвоём из Москвы в декабре")
        self.assertTrue(r.message.startswith("Люкс в бюджет"))
        self.assertIn("Готово", r.message)

    def test_day_of_date_in_words_is_not_invented(self):
        self.assertEqual(unsupported_numbers("с 14 по 21 ноября", {"dates": "14.11–21.11"}), [])
        self.assertEqual(unsupported_numbers("с 14 по 22 ноября", {"dates": "14.11–21.11"}), ["22"])


class LyingLLM(FakeLLM):
    """Пишет текст с ценой, которой нет в фактах."""

    def write(self, facts):
        return f"Отличный вариант, итого 99 999 ₽ — этап {facts['stage']}."


class HonestLLM(FakeLLM):
    def write(self, facts):
        return f"Собрал поездку, итого {facts['total_rub']:,} ₽.".replace(",", " ") if "total_rub" in facts else "Готово."


class WriterGuardTest(unittest.TestCase):
    def test_invented_number_falls_back_to_template(self):
        a = TravelAgent(llm=LyingLLM(), today=TODAY)
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы")
        self.assertNotIn("99 999", r.message)
        self.assertTrue(r.message.startswith("Поездка — Сочи"))
        self.assertIn("отброшен", next(s.detail for s in r.trace if s.step == "write"))

    def test_numbers_from_facts_pass(self):
        a = TravelAgent(llm=HonestLLM(), today=TODAY)
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы")
        self.assertEqual(r.message, f"Собрал поездку, итого {r.state.plan['total_rub']:,} ₽.".replace(",", " "))

    def test_number_check_handles_dates_times_and_spaces(self):
        facts = {"dates": "14.11–21.11", "outbound": {"departure": "2026-11-14T09:15", "duration_h": 4.0},
                 "total_rub": 175375}
        self.assertEqual(unsupported_numbers("14.11 в 09:15, 4 ч в пути, итого 175 375 ₽", facts), [])
        self.assertEqual(unsupported_numbers("итого 175 000 ₽, вода 26°", facts), ["175000", "26"])


class ApiError(Exception):
    def __init__(self, status_code=None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeClient:
    """Подмена openai.OpenAI: отвечает по сценарию в зависимости от режима запроса."""

    def __init__(self, tools=None, json_mode=None, text=None):
        self.answers = {"tools": tools, "json": json_mode, "text": text}
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        mode = "tools" if "tools" in kw else "json" if "response_format" in kw else "text"
        self.calls.append(mode)
        ans = self.answers[mode]
        if isinstance(ans, Exception):
            raise ans
        if mode == "tools":
            calls = [SimpleNamespace(function=SimpleNamespace(arguments=ans))] if ans else []
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=calls, content=None))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None, content=ans))])


def llm_with(client) -> LLM:
    llm = LLM()
    llm.enabled, llm.model, llm._client = True, "fake", client
    llm.fallback_model = llm.reasoning_effort = ""     # не зависеть от .env разработчика
    llm.retries, llm.retry_pause = 1, 0
    return llm


class LLMClientTest(unittest.TestCase):
    def test_falls_back_to_json_mode_and_remembers_it(self):
        client = FakeClient(tools=ApiError(400), json_mode='{"intent": "plan", "month": 11}')
        llm = llm_with(client)
        self.assertEqual(llm.extract_patch("в ноябре", "{}", "2026-10-25"), {"intent": "plan", "month": 11})
        self.assertEqual(llm.mode, "json")
        llm.extract_patch("в ноябре", "{}", "2026-10-25")
        self.assertEqual(client.calls, ["tools", "json", "json"])     # tools больше не пробуем

    def test_text_mode_extracts_json_from_prose(self):
        client = FakeClient(tools=ApiError(400), json_mode=ApiError(400),
                            text='Вот патч:\n```json\n{"intent": "plan", "adults": 2}\n```')
        self.assertEqual(llm_with(client).extract_patch("вдвоём", "{}", "2026-10-25"), {"intent": "plan", "adults": 2})

    def test_outage_pauses_llm_instead_of_waiting_every_turn(self):
        client = FakeClient(tools=ApiError(None))                       # таймаут / нет сети
        llm = llm_with(client)
        self.assertIsNone(llm.extract_patch("x", "{}", "2026-10-25"))
        self.assertEqual(llm.last_error, "HTTP None")                   # в trace видна настоящая причина
        self.assertIsNone(llm.write({"stage": "plan"}))
        self.assertIsNone(llm.extract_patch("y", "{}", "2026-10-25"))
        self.assertEqual(client.calls, ["tools"])                       # следующие вызовы не пошли в сеть
        self.assertIn("пауза", llm.last_error)

    def test_retries_main_model_on_503_before_fallback(self):
        client = FakeClient(tools='{"intent": "plan"}')
        orig, seen = client.create, []

        def create(**kw):                     # Polza: «не удалось проверить лимиты» — через секунду проходит
            seen.append(kw["model"])
            if len(seen) == 1:
                raise ApiError(503)
            return orig(**kw)

        client.chat.completions.create = create
        llm = llm_with(client)
        llm.model, llm.fallback_model = "main", "backup"
        self.assertEqual(llm.extract_patch("x", "{}", "2026-10-25"), {"intent": "plan"})
        self.assertEqual(seen, ["main", "main"])

    def test_gives_up_on_main_after_retries_and_uses_fallback(self):
        client = FakeClient(tools='{"intent": "plan"}')
        orig, seen = client.create, []

        def create(**kw):
            seen.append(kw["model"])
            if kw["model"] == "main":
                raise ApiError(503)
            return orig(**kw)

        client.chat.completions.create = create
        llm = llm_with(client)
        llm.model, llm.fallback_model = "main", "backup"
        self.assertEqual(llm.extract_patch("x", "{}", "2026-10-25"), {"intent": "plan"})
        self.assertEqual(seen, ["main", "main", "backup"])

    def test_rate_limit_goes_to_fallback_without_retry(self):
        client = FakeClient(tools='{"intent": "plan"}')
        orig, seen = client.create, []

        def create(**kw):
            seen.append(kw["model"])
            if kw["model"] == "main":
                raise ApiError(429)
            return orig(**kw)

        client.chat.completions.create = create
        llm = llm_with(client)
        llm.model, llm.fallback_model = "main", "backup"
        llm.extract_patch("x", "{}", "2026-10-25")
        self.assertEqual(seen, ["main", "backup"])

    def test_reasoning_effort_is_sent_only_when_set(self):
        seen = []
        client = FakeClient(tools='{"intent": "plan"}', text="Готово.")
        orig = client.create
        client.chat.completions.create = lambda **kw: seen.append(kw.get("extra_body")) or orig(**kw)
        llm = llm_with(client)
        llm.extract_patch("x", "{}", "2026-10-25")
        llm.reasoning_effort = "none"                                   # Polza/OpenRouter: без рассуждений быстрее
        self.assertEqual(llm.write({"stage": "plan"}), "Готово.")
        self.assertEqual(seen, [None, {"reasoning": {"effort": "none"}}])

    def test_fallback_without_tools_does_not_downgrade_primary_mode(self):
        client = FakeClient(tools='{"intent": "plan"}')
        orig = client.create

        def create(**kw):                     # основная перегружена, запасная не умеет tools
            if kw["model"] == "main" and not client.calls:
                client.calls.append("429")
                raise ApiError(429)
            if kw["model"] == "backup" and "tools" in kw:
                raise ApiError(400)
            return orig(**kw)

        client.chat.completions.create = create
        llm = llm_with(client)
        llm.model, llm.fallback_model = "main", "backup"
        self.assertIsNone(llm.extract_patch("x", "{}", "2026-10-25"))
        self.assertEqual(llm.mode, "tools")                             # режим основной модели не испорчен
        self.assertIn("429", llm.last_error)

    def test_garbage_from_model_is_dropped(self):
        raw = {"intent": "plan", "month": "13", "adults": "2", "start": "завтра", "children_ages": [5, "x", 40],
               "transport": "телепорт", "budget_total_rub": "150000", "hack": 1}
        self.assertEqual(clean_patch(raw), {"intent": "plan", "adults": 2, "children_ages": [5],
                                            "budget_total_rub": 150000})
        self.assertEqual(clean_patch({"name": "update_trip_state", "arguments": {"intent": "checkout"}}),
                         {"intent": "checkout"})

    def test_agent_survives_llm_outage_mid_scenario(self):
        client = FakeClient(tools='{"intent": "plan", "destination_name": "Сочи", "month": 11, "adults": 2, '
                                  '"origin_city": "Москва"}')
        a = TravelAgent(llm=llm_with(client), today=TODAY)
        r = a.handle(message="Сочи в ноябре вдвоём из Москвы")
        self.assertEqual(r.stage, "plan")
        client.answers["tools"] = ApiError(None)                        # API упал посреди демо
        r = a.handle(r.session_id, message="а если на 2 дня позже?")
        self.assertEqual(r.stage, "plan")
        self.assertTrue(any(d["field"] == "dates" for d in r.diff))
        self.assertIn("LLM не ответила", r.trace[0].detail)


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
