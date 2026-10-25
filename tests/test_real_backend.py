"""Адаптер бэкенда участника 2 (tools/real_backend.py). Нужен ../hackathon-backend, иначе тесты пропускаются.

    python -m unittest tests.test_real_backend -v
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["AGENT_OFFLINE"] = "1"

import logging  # noqa: E402

logging.disable(logging.WARNING)

from tools import REQUIRED_FUNCTIONS, get_backend  # noqa: E402
from tools.validator import validate_plan  # noqa: E402

try:
    from tools import real_backend as rb  # noqa: E402
except ImportError as e:              # бэкенда нет рядом — пропускаем
    rb, SKIP = None, str(e)
else:
    SKIP = ""

TODAY = date(2026, 10, 25)
DAY = date(2026, 11, 14)


def use_backend(name: str | None) -> None:
    if name:
        os.environ["TOOLS_BACKEND"] = name
    else:
        os.environ.pop("TOOLS_BACKEND", None)
    get_backend.cache_clear()


@unittest.skipIf(rb is None, SKIP)
class AdapterContractTest(unittest.TestCase):
    def test_implements_contract(self):
        self.assertEqual([f for f in REQUIRED_FUNCTIONS if not hasattr(rb, f)], [])

    def test_destinations_cover_all_his_cities_with_climate(self):
        his = set(rb.hb_flights.FLIGHT_HOURS_FROM_MOSCOW)
        self.assertEqual({d["name"] for d in rb.DESTINATIONS}, his)
        self.assertEqual(len(rb.DESTINATIONS), 25)
        for d in rb.DESTINATIONS:
            self.assertEqual(sorted(d["climate"]), list(range(1, 13)), d["name"])
        dests = rb.search_destinations("Москва", 11, ["sea"], None, None, 2, 7)
        self.assertEqual(len(dests), 25)                               # Москвы среди его направлений нет
        sochi = next(d for d in dests if d["id"] == "sochi")
        self.assertEqual(sochi["flight_hours"], rb.hb_flights.FLIGHT_HOURS_FROM_MOSCOW["Сочи"])
        self.assertGreater(sochi["est_total_rub"], 0)
        self.assertIsNotNone(sochi["train_hours"])

    def test_eu_destinations_say_there_are_no_direct_flights(self):
        dests = {d["id"]: d for d in rb.search_destinations("Москва", 6, [], None, None, 2, 7)}
        for dest_id in ("rhodes", "heraklion", "barcelona", "malaga", "rome", "venice"):
            self.assertIn("прямых рейсов из России сейчас нет", dests[dest_id]["travel_note"])
        self.assertIsNone(dests["antalya"]["travel_note"])
        self.assertTrue(all(o["note"] for o in rb.search_transport("Москва", "rome", DAY, 2, "out")))

    def test_text_lookup_understands_cases_and_avoids_false_hits(self):
        self.assertEqual(rb.find_destination_by_text("в анталью")["id"], "antalya")
        self.assertEqual(rb.find_destination_by_text("в анталию")["id"], "antalya")      # частая опечатка
        self.assertEqual(rb.find_destination_by_text("в риме")["id"], "rome")
        self.assertEqual(rb.find_destination_by_text("в турцию")["id"], "antalya")
        self.assertIsNone(rb.find_destination_by_text("примерно в ноябре"))   # «рим» внутри слова
        self.assertIsNone(rb.find_destination_by_text("на марс"))

    def test_major_russian_origins_and_never_raises(self):
        for text, city in (("москвы", "Москва"), ("питера", "Санкт-Петербург"), ("казани", "Казань"),
                           ("екатеринбурга", "Екатеринбург"), ("новосибирска", "Новосибирск"),
                           ("нижнего новгорода", "Нижний Новгород")):
            self.assertEqual(rb.normalize_origin(text), city)
        self.assertIsNone(rb.normalize_origin("владивостока"))         # данных нет — агент скажет честно
        self.assertEqual(rb.search_transport("Владивосток", "sochi", DAY, 2, "out"), [])
        self.assertEqual(rb.search_transport("Москва", "атлантида", DAY, 2, "out"), [])
        self.assertEqual(rb.search_transport("Санкт-Петербург", "spb", DAY, 2, "out"), [])
        self.assertEqual(rb.search_hotels("sochi", DAY, DAY, 2, 0, 0), [])
        self.assertEqual(rb.search_transfers("sochi", "airport", "hotel:нет-такого", "", 2, 0), [])

    def test_regional_flight_is_moscow_schedule_plus_offset_in_local_time(self):
        msk = rb.search_transport("Москва", "antalya", DAY, 1, "out")
        nsk = rb.search_transport("Новосибирск", "antalya", DAY, 1, "out")
        self.assertEqual(len(nsk), len([o for o in msk if o["mode"] == "plane"]))
        for m, n in zip(msk, nsk):
            self.assertEqual(n["from"], "Новосибирск")
            self.assertEqual(n["departure"], m["departure"])             # то же расписание, местное время
            self.assertAlmostEqual(n["duration_h"], m["duration_h"] + 3.0, delta=0.06)
            self.assertEqual((n["departure_utc_offset"], n["arrival_utc_offset"]), (7, 3))
            wall = (datetime.fromisoformat(n["arrival"]) - datetime.fromisoformat(n["departure"])).total_seconds() / 3600
            self.assertAlmostEqual(wall + 4, n["duration_h"], delta=0.06)   # в Анталье на 4 часа раньше
            self.assertGreater(n["price_rub"], m["price_rub"])
            self.assertNotEqual(n["id"], m["id"])
        back = rb.search_transport("Екатеринбург", "antalya", date(2026, 11, 21), 1, "back")
        self.assertTrue(back)
        self.assertEqual((back[0]["to"], back[0]["arrival_utc_offset"]), ("Екатеринбург", 5))
        self.assertFalse([o for o in rb.search_transport("Казань", "sochi", DAY, 1, "out") if o["mode"] == "train"])


@unittest.skipIf(rb is None, SKIP)
class FieldMappingTest(unittest.TestCase):
    def test_hotel_fields(self):
        his = {h.id: h for h in rb._hotels()["Сочи"]}
        hotels = rb.search_hotels("sochi", DAY, date(2026, 11, 21), 2, 0, 0)
        self.assertEqual(len(hotels), len(his))
        for h in hotels:
            src = his[h["id"]]
            self.assertEqual(h["price_total_rub"], src.price_per_night * 7)
            self.assertEqual(h["meals"], {"none": "RO", "breakfast": "BB", "all_inclusive": "AI"}[src.meals])
            self.assertEqual(h["beach_m"], round(src.beach_km * 1000))
            self.assertEqual(h["max_guests"], src.capacity)
            self.assertRegex(h["check_in"], r"^\d\d:\d\d$")
        rome = rb.search_hotels("rome", DAY, date(2026, 11, 21), 2, 0, 0)
        self.assertTrue(all(h["beach_m"] is None for h in rome))     # у него beach_km есть и в Риме

    def test_kids_not_allowed_are_filtered_only_with_children(self):
        all_ = rb.search_hotels("antalya", DAY, date(2026, 11, 21), 2, 0, 0)
        kids = rb.search_hotels("antalya", DAY, date(2026, 11, 21), 2, 1, 0)
        self.assertTrue(all(h["kids_allowed"] for h in kids))
        self.assertEqual(len(kids), sum(h["kids_allowed"] for h in all_))

    def test_transport_fields(self):
        his = rb.hb_flights.generate_flights("Москва", "Анталья", DAY)
        ours = rb.search_transport("Москва", "antalya", DAY, 3, "out")
        self.assertEqual(len(ours), len(his))
        for o, f in zip(ours, his):
            self.assertEqual(o["mode"], "plane")
            self.assertEqual(o["duration_h"], round(f.duration_hours, 1))
            self.assertEqual(o["price_rub"], f.price * 3)                # у него цена за человека
            self.assertEqual(o["baggage"], f.baggage_kg > 0)
        back = rb.search_transport("Москва", "antalya", date(2026, 11, 21), 3, "back")
        self.assertEqual((back[0]["from"], back[0]["to"]), ("Анталья", "Москва"))
        self.assertFalse({o["id"] for o in ours} & {o["id"] for o in back})   # его FL-ммдд-N повторяются

    def test_local_time_across_timezones(self):
        for dest_id, name in (("okinawa", "Окинава"), ("sanya", "Санья"), ("barcelona", "Барселона")):
            for o in rb.search_transport("Москва", dest_id, DAY, 1, "out"):
                wall = (datetime.fromisoformat(o["arrival"]) - datetime.fromisoformat(o["departure"])).total_seconds() / 3600
                shift = o["arrival_utc_offset"] - o["departure_utc_offset"]
                self.assertAlmostEqual(wall - shift, o["duration_h"], delta=0.06, msg=name)
        train = [o for o in rb.search_transport("Москва", "kaliningrad", DAY, 1, "out") if o["mode"] == "train"]
        self.assertTrue(train)
        self.assertEqual(train[0]["arrival_utc_offset"], 2)           # Калининград на час западнее

    def test_return_transfer_is_separate_and_trains_use_station(self):
        h = rb.search_hotels("spb", DAY, date(2026, 11, 21), 2, 0, 0)[0]
        tin = rb.search_transfers("spb", "airport", f"hotel:{h['id']}", "2026-11-14T10:00", 2, 0)
        tout = rb.search_transfers("spb", f"hotel:{h['id']}", "airport", "2026-11-21T18:00", 2, 0)
        self.assertFalse({t["id"] for t in tin} & {t["id"] for t in tout})
        self.assertEqual(tout[0]["route"], "отель → аэропорт")
        station = rb.search_transfers("spb", "station", f"hotel:{h['id']}", "2026-11-14T10:00", 2, 0)
        self.assertEqual(station[0]["route"], "вокзал → отель")
        self.assertNotEqual([t["minutes"] for t in station], [t["minutes"] for t in tin])
        self.assertTrue(all(t["pets_ok"] for t in rb.search_transfers("spb", "airport", f"hotel:{h['id']}", "", 2, 1)))

    def test_checkout_uses_his_mock_payment(self):
        order = rb.checkout({"items": [{"title": "отель", "price_rub": 1000}, {"title": "рейс", "price_rub": 500}],
                             "total_rub": 1500})
        self.assertEqual((order["status"], order["paid_rub"]), ("paid", 1500))
        self.assertTrue(order["order_id"].startswith("OD-"))


@unittest.skipIf(rb is None, SKIP)
class AgentOnRealBackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        use_backend("tools.real_backend")
        from agent import TravelAgent
        cls.TravelAgent = TravelAgent

    @classmethod
    def tearDownClass(cls):
        use_backend(None)

    def agent(self):
        return self.TravelAgent(today=TODAY)

    def test_far_east_plan_validates_in_local_time(self):
        r = self.agent().handle(message="Окинава в ноябре вдвоём из Москвы")
        self.assertEqual(r.stage, "plan")
        plan = r.state.plan
        self.assertFalse([i for i in plan["issues"] if i["level"] == "error"])
        # тот же валидатор на тех же данных: прилёт в местном времени против местного заселения
        self.assertEqual(validate_plan(plan, 2, 0, None), plan["issues"])
        # 08:57 МСК + 10.7 ч = 01:39 по Окинаве (UTC+9): ночь заезда, а не «прилёт днём» по московским часам
        self.assertLessEqual(plan["outbound"]["arrival"], f"{plan['check_in'][:8]}{int(plan['check_in'][8:]) + 1:02d}T04:30")
        self.assertIn("late_arrival", {i["code"] for i in plan["issues"]})

    def test_train_trip_uses_station_transfers(self):
        r = self.agent().handle(message="в Петербург на поезде на выходные в ноябре вдвоём из Москвы")
        plan = r.state.plan
        self.assertEqual(plan["outbound"]["mode"], "train")
        self.assertEqual(plan["transfer_in"]["route"], "вокзал → отель")
        self.assertEqual(plan["transfer_out"]["route"], "отель → вокзал")

    def test_eu_plan_warns_about_flights(self):
        r = self.agent().handle(message="Рим в ноябре вдвоём из Москвы")
        self.assertTrue(any(i["code"] == "route_note" for i in r.state.plan["issues"]))
        self.assertIn("прямых рейсов из россии сейчас нет", r.message.lower())

    def test_unsupported_origin_is_honest_and_offers_supported(self):
        r = self.agent().handle(message="из Владивостока в Сочи в ноябре вдвоём")
        self.assertIn("Владивосток", r.message)
        self.assertEqual(r.stage, "clarify")
        self.assertEqual([o.label for o in r.questions[0].options],
                         ["Москва", "Санкт-Петербург", "Казань", "Екатеринбург"])

    def test_regional_origin_plan(self):
        r = self.agent().handle(message="из Екатеринбурга в Сочи в ноябре вдвоём")
        self.assertEqual(r.stage, "plan")
        self.assertEqual(r.state.plan["outbound"]["from"], "Екатеринбург")
        self.assertFalse([i for i in r.state.plan["issues"] if i["level"] == "error"])

    def test_unknown_early_checkin_is_not_a_refusal(self):
        a = self.agent()
        r = a.handle(message="Сочи на неделю в ноябре вдвоём из Москвы")
        self.assertIsNone(r.state.plan["hotel"]["early_checkin"])    # в его данных этого нет
        r = a.handle(r.session_id, action={"type": "checkout"})
        r = a.handle(r.session_id, message="можно заселиться пораньше?")
        self.assertNotIn("не делает ранний заезд", r.message)        # «не знаем» ≠ «нет»
        self.assertIn("запрос", r.message)

    def top(self, r):
        return [c.data for c in r.cards if c.type == "destination"]

    def test_missing_city_same_country_first(self):
        r = self.agent().handle(message="Стамбул в ноябре вдвоём из Москвы")
        self.assertIn("По направлению «Стамбул» у меня нет данных", r.message)
        self.assertEqual(self.top(r)[0]["country"], "Турция")         # Анталья, Аланья или Бодрум

    def test_missing_country_same_region_and_trip_type(self):
        r = self.agent().handle(message="хочу на Пхукет в декабре вдвоём из Москвы")
        self.assertIn("По направлению «Пхукет» у меня нет данных", r.message)
        self.assertIn(self.top(r)[0]["name"], ("Санья", "Окинава"))   # море и природа в Азии, а не Эмираты

    def test_missing_ski_resort_is_honest(self):
        r = self.agent().handle(message="Шерегеш в январе вдвоём из Москвы")
        self.assertIn("По направлению «Шерегеш» у меня нет данных", r.message)
        self.assertIn("горнолыжных курортов", r.message.lower())
        self.assertEqual(self.top(r)[0]["country"], "Россия")

    def test_unknown_place_without_hints(self):
        r = self.agent().handle(message="хочу в Атлантиду в ноябре вдвоём из Москвы")
        self.assertIn("у меня нет данных — подбираю по остальным условиям", r.message)

    def test_ski_is_not_in_catalog(self):
        r = self.agent().handle(message="на лыжах в январе вдвоём из Москвы")
        self.assertIn("горнолыжных курортов", r.message.lower())

    def test_luxury_bonus_survives_hotels_without_stars(self):
        r = self.agent().handle(message="люкс в Чжуншань в ноябре вдвоём из Москвы")   # там звёзд нет ни у кого
        self.assertEqual(r.stage, "plan")


if __name__ == "__main__":
    unittest.main()
