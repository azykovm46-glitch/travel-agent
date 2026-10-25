"""Адаптер бэкенда участника 2 (../hackathon-backend) под контракт docs/CONTRACTS.md.

    TOOLS_BACKEND=tools.real_backend            # путь к бэкенду: HACKATHON_BACKEND_DIR

Его код не меняем, только вызываем: отели (OSM + синтетика), рейсы, поезда,
трансферы и оплата — из его src/. Направлений и погоды у него нет, поэтому
справочник DESTINATIONS ниже наш: климатические нормы приблизительные, время
перелёта — из его FLIGHT_HOURS_FROM_MOSCOW.

Что переводим в наш формат:
- price_per_night × ночи → price_total_rub; meals none/breakfast/all_inclusive → RO/BB/AI;
  beach_km → beach_m (только для направлений у моря); capacity → max_guests;
- mode flight → plane; duration_h из duration_hours (с учётом поясов); baggage_kg → baggage;
  цена билета у него за человека — умножаем на число путешественников;
- departure/arrival остаются в местном времени городов (так и считает наш валидатор:
  прилёт сравнивается с заселением, вылет — с выездом, всё в поясе направления);
  смещение UTC — в departure_utc_offset / arrival_utc_offset;
- трансфер обратно считаем отдельно (у него это копия трансфера «из аэропорта»),
  для поездов — от вокзала по реальным координатам отеля.

Рейсы у него генерируются только от Москвы: для других городов вылета генератор падает
(нет пояса) или подставляет время полёта из Москвы. Поэтому его генератор зовём только для пары
с Москвой, а вылет из Петербурга, Казани, Екатеринбурга, Новосибирска и Нижнего считаем от
московского рейса с поправкой ко времени (как в моке) и поясом города. Остальные города
normalize_origin не узнаёт, и агент честно говорит об этом.
"""
from __future__ import annotations

import importlib.util
import math
import os
import re
import statistics
import sys
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from tools.mock_backend import ORIGIN_ALIASES, ORIGIN_OFFSET

BACKEND_DIR = Path(os.getenv("HACKATHON_BACKEND_DIR") or Path(__file__).resolve().parents[2] / "hackathon-backend")
_SRC = BACKEND_DIR / "src"
_HIS_MODULES = ("models", "flights", "trains", "scoring", "validator", "cart")


def _load_backend():
    """Его модули импортируют друг друга по коротким именам, а его tools.py совпадает по имени
    с нашим пакетом tools — грузим его под другим именем."""
    if not (_SRC / "tools.py").exists():
        raise ImportError(f"Не найден бэкенд участника 2: {_SRC} (задайте HACKATHON_BACKEND_DIR)")
    clash = [m for m in _HIS_MODULES if m in sys.modules
             and not str(getattr(sys.modules[m], "__file__", "")).startswith(str(_SRC))]
    if clash:
        raise ImportError(f"Имена модулей бэкенда уже заняты: {', '.join(clash)}")
    sys.path.insert(0, str(_SRC))
    try:
        spec = importlib.util.spec_from_file_location("hackathon_backend_tools", _SRC / "tools.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SRC))
    return module


hb = _load_backend()
hb_flights, hb_trains, hb_models = (sys.modules[m] for m in ("flights", "trains", "models"))

DATA_SOURCE = "hackathon-backend: отели из OSM, цены и рейсы синтетические"
# Города вылета: пояс UTC. Его генератор знает только Москву — для остальных рейс считаем от московского
# (время в пути + ORIGIN_OFFSET мока, прилёт — в поясе города). Кроме Москвы, поездов нет.
ORIGIN_UTC = {"Москва": 3, "Санкт-Петербург": 3, "Казань": 3, "Екатеринбург": 5, "Новосибирск": 7,
              "Нижний Новгород": 3}
ORIGIN_SLUG = {"Москва": "mow", "Санкт-Петербург": "led", "Казань": "kzn", "Екатеринбург": "svx",
               "Новосибирск": "ovb", "Нижний Новгород": "goj"}
SUPPORTED_ORIGINS = list(ORIGIN_UTC)
MEALS = {"none": "RO", "breakfast": "BB", "all_inclusive": "AI"}
NO_DIRECT = "прямых рейсов из России сейчас нет — рейсы в демо синтетические, реальной доступности не означают"

# ---------------------------------------------------------------------------
# Справочник направлений. climate: месяц -> (воздух днём °C, вода °C или None) —
# приблизительные климатические нормы. center / station — координаты (широта, долгота).
# Имена совпадают с его данными (hotels.json, FLIGHT_HOURS_FROM_MOSCOW, UTC_OFFSET_HOURS).
# ---------------------------------------------------------------------------
_TR, _EG, _AE = "Турция", "Египет", "ОАЭ"
_SEA_TR = {1: (15, 17), 2: (16, 17), 3: (18, 17), 4: (22, 18), 5: (26, 21), 6: (31, 25),
           7: (34, 27), 8: (34, 28), 9: (31, 27), 10: (26, 24), 11: (21, 21), 12: (17, 18)}
_RED_SEA = {1: (22, 22), 2: (23, 21), 3: (25, 22), 4: (29, 24), 5: (33, 26), 6: (35, 27),
            7: (36, 28), 8: (36, 29), 9: (34, 28), 10: (31, 27), 11: (27, 25), 12: (23, 23)}
_GULF = {1: (24, 22), 2: (25, 21), 3: (28, 22), 4: (33, 24), 5: (38, 28), 6: (40, 30),
         7: (41, 32), 8: (41, 33), 9: (39, 31), 10: (35, 29), 11: (30, 26), 12: (26, 23)}
_BLACK_SEA = {1: (11, 10), 2: (11, 9), 3: (13, 10), 4: (17, 12), 5: (21, 17), 6: (25, 22),
              7: (28, 25), 8: (28, 26), 9: (25, 23), 10: (20, 19), 11: (16, 15), 12: (12, 12)}


def _city(air: list[int], water: list[int] | None = None) -> dict[int, tuple]:
    return {m + 1: (air[m], water[m] if water else None) for m in range(12)}


DESTINATIONS: list[dict] = [
    {"id": "antalya", "name": "Анталья", "country": _TR, "patterns": [r"\bантал[ьи]"],
     "tags": ["sea", "city", "all_inclusive"], "visa": "не нужна", "airport_km": 13,
     "center": (36.8969, 30.7133), "climate": _SEA_TR},
    {"id": "alanya", "name": "Аланья", "country": _TR, "patterns": [r"\bалань"],
     "tags": ["sea", "all_inclusive"], "visa": "не нужна", "airport_km": 40,
     "center": (36.5444, 31.9954), "climate": _SEA_TR},
    {"id": "bodrum", "name": "Бодрум", "country": _TR, "patterns": [r"\bбодрум"],
     "tags": ["sea", "food"], "visa": "не нужна", "airport_km": 36, "center": (37.0344, 27.4305),
     "climate": _city([15, 15, 17, 20, 25, 29, 32, 32, 29, 25, 20, 16], [17, 16, 16, 17, 20, 23, 25, 25, 24, 22, 20, 18])},
    {"id": "hurghada", "name": "Хургада", "country": _EG, "patterns": [r"\bхургад"],
     "tags": ["sea", "all_inclusive", "diving"], "visa": "по прилёте", "airport_km": 6,
     "center": (27.2579, 33.8116), "climate": _RED_SEA},
    {"id": "sharm", "name": "Шарм-эль-Шейх", "country": _EG, "patterns": [r"\bшарм"],
     "tags": ["sea", "all_inclusive", "diving"], "visa": "не нужна (только Южный Синай, до 15 дней)",
     "airport_km": 15, "center": (27.9158, 34.3300), "climate": _RED_SEA},
    {"id": "sahl_hasheesh", "name": "Сахл-Хашиш", "country": _EG, "patterns": [r"\bсахл"],
     "tags": ["sea", "all_inclusive"], "visa": "по прилёте", "airport_km": 20,
     "center": (27.0400, 33.8800), "climate": _RED_SEA},
    {"id": "sochi", "name": "Сочи", "country": "Россия", "patterns": [r"\bсочи", r"\bадлер"],
     "tags": ["sea", "nature", "city"], "visa": "не нужна", "airport_km": 30,
     "center": (43.5855, 39.7231), "station": (43.5906, 39.7186), "climate": _BLACK_SEA},
    {"id": "spb", "name": "Санкт-Петербург", "country": "Россия", "patterns": [r"\bпетербург", r"\bпитер", r"\bспб\b"],
     "tags": ["city", "culture"], "visa": "не нужна", "airport_km": 20,
     "center": (59.9343, 30.3351), "station": (59.9296, 30.3622),
     "climate": _city([-4, -4, 1, 9, 16, 20, 23, 21, 15, 9, 3, -1])},
    {"id": "kaliningrad", "name": "Калининград", "country": "Россия", "patterns": [r"\bкалининград"],
     "tags": ["city", "sea"], "visa": "не нужна", "airport_km": 22,
     "center": (54.7104, 20.4522), "station": (54.6927, 20.4996),
     "climate": _city([1, 1, 5, 12, 17, 21, 23, 23, 18, 12, 6, 2], [4, 3, 3, 6, 11, 16, 19, 19, 16, 12, 8, 5])},
    {"id": "anapa", "name": "Анапа", "country": "Россия", "patterns": [r"\bанап"],
     "tags": ["sea"], "visa": "не нужна", "airport_km": 10, "center": (44.8950, 37.3163),
     "climate": _city([5, 6, 10, 16, 22, 27, 30, 30, 25, 18, 12, 7], [8, 7, 8, 11, 17, 22, 25, 25, 22, 17, 13, 10])},
    {"id": "tuapse", "name": "Туапсе", "country": "Россия", "patterns": [r"\bтуапсе"],
     "tags": ["sea", "nature"], "visa": "не нужна", "airport_km": 110,   # аэропорта нет, ближайший — Сочи
     "center": (44.1000, 39.0700), "climate": _BLACK_SEA},
    {"id": "minsk", "name": "Минск", "country": "Беларусь", "patterns": [r"\bминск", r"\bбеларус", r"\bбелорус"],
     "tags": ["city", "culture"], "visa": "не нужна", "airport_km": 42,
     "center": (53.9045, 27.5615), "station": (53.8906, 27.5510),
     "climate": _city([-3, -2, 3, 12, 18, 21, 23, 22, 17, 10, 3, -1])},
    {"id": "sanya", "name": "Санья", "country": "Китай", "patterns": [r"\bсань[яеиюй]", r"\bхайнан"],
     "tags": ["sea", "nature"], "visa": "не нужна на Хайнане (до 30 дней) — проверьте актуальные правила",
     "airport_km": 15, "center": (18.2528, 109.5119),
     "climate": _city([26, 27, 29, 31, 32, 32, 32, 32, 31, 30, 28, 26], [24, 24, 25, 27, 29, 30, 30, 29, 29, 28, 27, 25])},
    {"id": "zhongshan", "name": "Чжуншань", "country": "Китай", "patterns": [r"\bчжуншан"],
     "tags": ["city", "food"], "visa": "уточните актуальные правила въезда в Китай", "airport_km": 60,
     "center": (22.5170, 113.3926), "climate": _city([18, 19, 21, 25, 29, 31, 32, 32, 31, 29, 25, 20])},
    {"id": "dubai", "name": "Дубай", "country": _AE, "patterns": [r"\bдуба[йеяю]", r"\bоаэ\b", r"\bэмират"],
     "tags": ["sea", "city", "luxury"], "visa": "не нужна", "airport_km": 15,
     "center": (25.2048, 55.2708), "climate": _GULF},
    {"id": "abu_dhabi", "name": "Абу-Даби", "country": _AE, "patterns": [r"\bабу[- ]?даби"],
     "tags": ["sea", "city", "culture"], "visa": "не нужна", "airport_km": 30,
     "center": (24.4539, 54.3773), "climate": _GULF},
    {"id": "ras_al_khaimah", "name": "Рас-эль-Хайма", "country": _AE, "patterns": [r"\bрас[- ]?эль[- ]?хайм"],
     "tags": ["sea", "nature"], "visa": "не нужна", "airport_km": 20,
     "center": (25.8007, 55.9762), "climate": _GULF},
    {"id": "muscat", "name": "Маскат", "country": "Оман", "patterns": [r"\bмаскат", r"\bоман"],
     "tags": ["sea", "culture", "nature"], "visa": "не нужна (до 14 дней)", "airport_km": 30,
     "center": (23.5880, 58.3829),
     "climate": _city([25, 26, 29, 34, 38, 40, 38, 36, 36, 35, 30, 27], [24, 23, 24, 26, 29, 30, 29, 28, 28, 28, 27, 25])},
    {"id": "okinawa", "name": "Окинава", "country": "Япония", "patterns": [r"\bокинав", r"\bнаха\b", r"\bяпони"],
     "tags": ["sea", "nature", "food"], "visa": "нужна (электронная)", "airport_km": 5,
     "center": (26.2124, 127.6809),
     "climate": _city([19, 19, 21, 24, 27, 30, 32, 31, 30, 27, 24, 21], [22, 21, 22, 23, 25, 27, 29, 29, 28, 27, 25, 23])},
    {"id": "rhodes", "name": "Родос", "country": "Греция", "patterns": [r"\bродос"],
     "tags": ["sea", "culture"], "visa": "шенгенская", "airport_km": 14, "center": (36.4349, 28.2176),
     "note": NO_DIRECT,
     "climate": _city([15, 16, 18, 21, 25, 29, 31, 32, 29, 25, 20, 17], [17, 16, 17, 18, 20, 23, 25, 26, 25, 23, 20, 18])},
    {"id": "heraklion", "name": "Ираклион", "country": "Греция", "patterns": [r"\bираклион", r"\bкрит(?!ик|ич)"],
     "tags": ["sea", "culture", "food"], "visa": "шенгенская", "airport_km": 5, "center": (35.3387, 25.1442),
     "note": NO_DIRECT,
     "climate": _city([16, 16, 18, 21, 24, 28, 29, 29, 27, 24, 21, 18], [17, 16, 16, 17, 20, 23, 25, 25, 25, 23, 21, 18])},
    {"id": "barcelona", "name": "Барселона", "country": "Испания", "patterns": [r"\bбарселон"],
     "tags": ["city", "culture", "sea", "food"], "visa": "шенгенская", "airport_km": 15,
     "center": (41.3874, 2.1686), "note": NO_DIRECT,
     "climate": _city([14, 15, 17, 19, 22, 26, 29, 29, 26, 22, 17, 14], [13, 13, 13, 14, 17, 21, 24, 25, 23, 20, 17, 14])},
    {"id": "malaga", "name": "Малага", "country": "Испания", "patterns": [r"\bмалаг"],
     "tags": ["sea", "city", "food"], "visa": "шенгенская", "airport_km": 9, "center": (36.7213, -4.4214),
     "note": NO_DIRECT,
     "climate": _city([17, 18, 20, 22, 25, 28, 31, 31, 28, 24, 20, 18], [16, 15, 15, 16, 18, 20, 22, 23, 22, 20, 18, 17])},
    {"id": "rome", "name": "Рим", "country": "Италия", "patterns": [r"\bрим(ом|[еуа])?\b"],
     "tags": ["city", "culture", "food"], "visa": "шенгенская", "airport_km": 30, "center": (41.9028, 12.4964),
     "note": NO_DIRECT, "climate": _city([12, 14, 17, 20, 24, 29, 32, 32, 28, 23, 17, 13])},
    {"id": "venice", "name": "Венеция", "country": "Италия", "patterns": [r"\bвенеци"],
     "tags": ["city", "culture"], "visa": "шенгенская", "airport_km": 13, "center": (45.4408, 12.3155),
     "note": NO_DIRECT, "climate": _city([6, 9, 13, 17, 22, 26, 29, 28, 24, 18, 12, 7])},
]
# Страна без города — самое популярное направление в ней. Проверяется после городов.
COUNTRY_PATTERNS = [(r"\bтурци", "antalya"), (r"\bегип", "hurghada"), (r"\bкита", "sanya"),
                    (r"\bгреци", "rhodes"), (r"\bиспани", "barcelona"), (r"\bитали", "rome")]
_BY_ID = {d["id"]: d for d in DESTINATIONS}
_BY_NAME = {d["name"]: d for d in DESTINATIONS}


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    return hb_trains.distance_km(a, b)


@lru_cache(maxsize=1)
def _hotels() -> dict[str, list]:
    """Его load_hotels() читает JSON на каждый вызов — читаем один раз."""
    by_dest: dict[str, list] = {}
    for h in hb.load_hotels():
        by_dest.setdefault(h.destination, []).append(h)
    return by_dest


def _hotel(hotel_id: str):
    return next((h for hs in _hotels().values() for h in hs if h.id == hotel_id), None)


def _public(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in ("patterns", "climate", "center", "station")}


# ---------------------------------------------------------------------------
# Справочник
# ---------------------------------------------------------------------------
def get_destination(dest_id: str) -> dict | None:
    d = _BY_ID.get(dest_id)
    return _public(d) if d else None


def find_destination_by_text(text: str) -> dict | None:
    t = (text or "").lower().replace("ё", "е")
    for d in DESTINATIONS:
        if any(re.search(p, t) for p in d["patterns"]):
            return _public(d)
    for pat, dest_id in COUNTRY_PATTERNS:
        if re.search(pat, t):
            return get_destination(dest_id)
    return None


def normalize_origin(text: str) -> str | None:
    t = (text or "").lower()
    city = next((v for k, v in ORIGIN_ALIASES.items() if k in t), None)
    return city if city in SUPPORTED_ORIGINS else None


# ---------------------------------------------------------------------------
# Инструменты
# ---------------------------------------------------------------------------
def _month_day(month: int) -> date:
    today = date.today()
    year = today.year if month >= today.month else today.year + 1
    return date(year, month, 15)


def search_destinations(origin: str, month: int, trip_type: list[str],
                        max_travel_hours: float | None, budget_per_night: int | None,
                        travelers: int, nights: int) -> list[dict]:
    """Все направления с погодой, временем в пути и оценкой цены по его данным:
    медиана цены отеля за ночь и самый дешёвый рейс на 15-е число месяца."""
    try:
        day = _month_day(month)
        out = []
        for d in DESTINATIONS:
            if d["name"] == origin:
                continue
            air, water = d["climate"][month]
            hours = round(hb_flights.FLIGHT_HOURS_FROM_MOSCOW[d["name"]] + ORIGIN_OFFSET.get(origin, 0.0), 1)
            prices = [h.price_per_night for h in _hotels().get(d["name"], [])]
            night = int(statistics.median(prices)) if prices else 0
            fares = [o["price_rub"] for o in search_transport(origin if origin in SUPPORTED_ORIGINS else "Москва",
                                                              d["id"], day, 1) if o["mode"] == "plane"]
            flight_rub = (min(fares) if fares else 0) * travelers
            train = "station" in d and origin == "Москва"
            out.append({
                "id": d["id"], "name": d["name"], "country": d["country"], "tags": d["tags"],
                "visa": d["visa"], "air_temp": air, "water_temp": water,
                "flight_hours": hours,
                "train_hours": round(_km(hb_trains.RAIL_CITIES["Москва"], hb_trains.RAIL_CITIES[d["name"]])
                                     / hb_trains.AVG_SPEED_KMH, 1) if train else None,
                "est_total_rub": flight_rub * 2 + night * nights * max(1, (travelers + 1) // 2),
                "est_night_rub": night,
                "ski_season": False,
                "travel_note": d.get("note"),
                "data_source": DATA_SOURCE,
            })
        return out
    except Exception:
        return []


def get_weather(dest_id: str, month: int) -> dict:
    d = _BY_ID.get(dest_id)
    if not d or month not in d["climate"]:
        return {}
    air, water = d["climate"][month]
    return {"dest_id": dest_id, "month": month, "air_temp": air, "water_temp": water,
            "data_source": "справочник адаптера (климатическая норма, приблизительно)"}


def _transport(f, direction: str, dest_id: str, travelers: int, note: str | None, origin: str = "Москва") -> dict:
    """Его рейс (всегда Москва ↔ направление) в наш формат. Для другого города вылета берём московское
    расписание и добавляем ORIGIN_OFFSET мока ко времени в пути, а прилёт считаем в поясе этого города."""
    utc = {**hb_models.UTC_OFFSET_HOURS, **ORIGIN_UTC}
    base = f.duration_hours
    hours = base + ORIGIN_OFFSET[origin]
    src, dst = (origin, f.destination) if direction == "out" else (f.origin, origin)
    arrives = f.departs + timedelta(hours=hours + utc[dst] - utc[src])
    tag = "" if origin == "Москва" else f"{ORIGIN_SLUG[origin]}-"
    return {
        # его id вида FL-1114-1 повторяются на разных маршрутах — делаем уникальным
        "id": f"rb-{direction}-{dest_id}-{tag}{f.id}", "mode": "plane" if f.mode == "flight" else "train",
        "carrier": f.id, "from": src, "to": dst,
        "departure": f.departs.isoformat(timespec="minutes"), "arrival": arrives.isoformat(timespec="minutes"),
        "departure_utc_offset": utc[src], "arrival_utc_offset": utc[dst],
        "duration_h": round(hours, 1), "stops": f.stops,
        "baggage": f.baggage_kg > 0 or f.mode == "train", "baggage_kg": f.baggage_kg,
        "price_rub": int(round(f.price * hours / base, -2)) * travelers,     # дольше лететь — дороже, как в моке
        "note": note, "synthetic": f.synthetic,
        "data_source": DATA_SOURCE + ("" if origin == "Москва" else "; рейс рассчитан от московского с поправкой"),
    }


def search_transport(origin: str, dest_id: str, day: date, travelers: int,
                     direction: str = "out") -> list[dict]:
    d = _BY_ID.get(dest_id)
    if not d or origin not in SUPPORTED_ORIGINS or d["name"] == origin:
        return []
    # его генератор вызываем только для пары с Москвой: для других городов он падает или ошибается
    src, dst = ("Москва", d["name"]) if direction == "out" else (d["name"], "Москва")
    try:
        found = hb_flights.generate_flights(src, dst, day)
        if origin == "Москва":
            found += hb_trains.generate_trains(src, dst, day)     # поезда у него только от Москвы
    except Exception:
        return []
    return [_transport(f, direction, dest_id, travelers, d.get("note"), origin) for f in found]


def _hotel_dict(h, d: dict, nights: int) -> dict:
    return {
        "id": h.id, "name": h.name, "dest_id": d["id"], "stars": h.stars,
        "price_per_night_rub": h.price_per_night, "price_total_rub": h.price_per_night * nights, "nights": nights,
        "meals": MEALS.get(h.meals, "RO"),
        "free_cancellation": h.cancel_free_days > 0, "cancellation_deadline_days": h.cancel_free_days,
        "max_guests": h.capacity, "family_rooms": h.kids_allowed and h.capacity >= 4,
        "pets_allowed": h.pets_allowed, "kids_allowed": h.kids_allowed, "kids_club": False,
        # у него beach_km есть у всех, даже в Риме и Минске — у городов без моря не показываем
        "beach_m": round(h.beach_km * 1000) if "sea" in d["tags"] else None,
        "center_km": round(_km((h.lat, h.lon), d["center"]), 1),
        "airport_km": d["airport_km"],
        "check_in": h.checkin.strftime("%H:%M"), "check_out": h.checkout.strftime("%H:%M"),
        "early_checkin": None,                       # в его данных нет: не «нет», а «неизвестно»
        "rating": h.rating, "reviews": None, "review_pros": [], "review_cons": [],
        "address": h.address, "website": h.website, "phone": h.phone, "lat": h.lat, "lon": h.lon,
        "data_source": DATA_SOURCE,
    }


def search_hotels(dest_id: str, check_in: date, check_out: date, adults: int,
                  children: int, pets: int) -> list[dict]:
    """Все отели направления. Вместимость и питомцев фильтрует наш скоринг (с несколькими
    номерами), а «дети не допускаются» — только здесь, как в его hard_filter_hotel."""
    d = _BY_ID.get(dest_id)
    nights = (check_out - check_in).days
    if not d or nights <= 0:
        return []
    return [_hotel_dict(h, d, nights) for h in _hotels().get(d["name"], [])
            if h.kids_allowed or not children]


def _point(p: str) -> str:
    return {"airport": "аэропорт", "station": "вокзал"}.get(p, "отель")


def search_transfers(dest_id: str, from_point: str, to_point: str, when: str,
                     travelers: int, pets: int) -> list[dict]:
    """from_point / to_point: airport | station | hotel:<id>.
    Аэропорт — его формула; обратно — отдельные варианты с маршрутом «отель → аэропорт».
    Вокзал — у него нет: считаем по расстоянию от вокзала до отеля (~20 км/ч по городу)."""
    d = _BY_ID.get(dest_id)
    hotel_id = next((p.split(":", 1)[1] for p in (from_point, to_point) if p.startswith("hotel:")), None)
    h = _hotel(hotel_id) if hotel_id else None
    if not d or not h:
        return []
    back = from_point.startswith("hotel:")
    hub = to_point if back else from_point
    route = f"{_point(from_point)} → {_point(to_point)}"
    if hub == "station" and "station" in d:
        km = _km((h.lat, h.lon), d["station"])
        opts = [{"kind": "taxi", "minutes": int(10 + km * 3), "price_rub": int(400 + 30 * km) * (1 if travelers <= 4 else 2),
                 "pets_ok": True},
                {"kind": "public", "minutes": int(15 + km * 5), "price_rub": 60 * travelers, "pets_ok": False}]
        opts = [{**o, "id": f"rb-tf-{h.id}-station-{'back' if back else 'in'}-{o['kind']}"} for o in opts]
    else:
        opts = [{"id": f"rb-{t.id}-{'back' if back else 'in'}", "kind": t.kind, "minutes": t.duration_min,
                 "price_rub": t.price * (2 if t.kind == "taxi" and travelers > 4 else 1),
                 "pets_ok": t.kind == "taxi"} for t in hb.search_transfers(h)]
    for o in opts:
        o.update({"from": from_point, "to": to_point, "route": route, "when": when,
                  "synthetic": True, "data_source": DATA_SOURCE})
    return [o for o in opts if o["pets_ok"] or not pets]


def checkout(cart: dict) -> dict:
    """Его имитация оплаты: позиции (название, цена), карта 4242 — оплата проходит."""
    try:
        order = hb.checkout([(i["title"], int(i["price_rub"])) for i in cart.get("items", [])],
                            cart.get("card_last4", "4242"))
    except Exception:
        return {"order_id": None, "status": "error", "paid_rub": 0, "payment": "имитация"}
    return {"order_id": order.booking_number, "status": "paid" if order.status == "confirmed" else order.status,
            "paid_rub": order.total, "payment": "имитация", "voucher": order.voucher_code,
            "data_source": DATA_SOURCE}
