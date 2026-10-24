"""Моковый бэкенд: направления, рейсы, поезда, отели, трансферы.

ВСЕ ДАННЫЕ СИНТЕТИЧЕСКИЕ (честно говорим об этом на демо).
Генерация детерминированная: одинаковый запрос -> одинаковая выдача.
Участник 2 заменяет этот модуль реальным бэкендом с тем же интерфейсом
(см. tools/__init__.py и docs/CONTRACTS.md).
"""
from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Справочник направлений (приблизительные климатические и ценовые данные)
# climate: месяц -> (воздух днём °C, вода °C или None)
# ---------------------------------------------------------------------------
DESTINATIONS: list[dict] = [
    {"id": "sochi", "name": "Сочи", "country": "Россия", "aliases": ["сочи", "адлер"],
     "tags": ["sea", "nature", "city"], "night_rub": 7000, "visa": "не нужна",
     "hours_from_moscow": 2.5, "train_hours_from_moscow": 24, "airport_km": 30, "station_km": 3,
     "climate": {1: (11, 10), 2: (11, 9), 3: (13, 10), 4: (17, 12), 5: (21, 17), 6: (25, 22),
                 7: (28, 25), 8: (28, 26), 9: (25, 23), 10: (20, 19), 11: (16, 15), 12: (12, 12)}},
    {"id": "kaliningrad", "name": "Калининград", "country": "Россия", "aliases": ["калининград"],
     "tags": ["city", "sea"], "night_rub": 5500, "visa": "не нужна",
     "hours_from_moscow": 2.2, "train_hours_from_moscow": None, "airport_km": 22, "station_km": 2,
     "climate": {1: (1, 4), 2: (1, 3), 3: (5, 3), 4: (12, 6), 5: (17, 11), 6: (21, 16),
                 7: (23, 19), 8: (23, 19), 9: (18, 16), 10: (12, 12), 11: (6, 8), 12: (2, 5)}},
    {"id": "kazan", "name": "Казань", "country": "Россия", "aliases": ["казань"],
     "tags": ["city", "culture"], "night_rub": 5000, "visa": "не нужна",
     "hours_from_moscow": 1.5, "train_hours_from_moscow": 12, "airport_km": 26, "station_km": 2,
     "climate": {1: (-9, None), 2: (-8, None), 3: (-1, None), 4: (10, None), 5: (19, None),
                 6: (23, None), 7: (25, None), 8: (23, None), 9: (16, None), 10: (8, None),
                 11: (-1, None), 12: (-7, None)}},
    {"id": "spb", "name": "Санкт-Петербург", "country": "Россия", "aliases": ["петербург", "питер", "спб"],
     "tags": ["city", "culture"], "night_rub": 6500, "visa": "не нужна",
     "hours_from_moscow": 1.4, "train_hours_from_moscow": 4, "airport_km": 18, "station_km": 2,
     "climate": {1: (-4, None), 2: (-4, None), 3: (1, None), 4: (9, None), 5: (16, None),
                 6: (20, None), 7: (23, None), 8: (21, None), 9: (15, None), 10: (8, None),
                 11: (2, None), 12: (-2, None)}},
    {"id": "antalya", "name": "Анталья", "country": "Турция", "aliases": ["анталья", "анталия", "турция"],
     "tags": ["sea", "all_inclusive"], "night_rub": 9000, "visa": "не нужна (до 60 дней)",
     "hours_from_moscow": 4.0, "train_hours_from_moscow": None, "airport_km": 15, "station_km": None,
     "climate": {1: (15, 17), 2: (16, 16), 3: (18, 17), 4: (21, 18), 5: (26, 22), 6: (31, 25),
                 7: (34, 28), 8: (34, 29), 9: (31, 27), 10: (26, 24), 11: (21, 21), 12: (17, 19)}},
    {"id": "sharm", "name": "Шарм-эль-Шейх", "country": "Египет", "aliases": ["шарм", "египет", "египт"],
     "tags": ["sea", "all_inclusive", "diving"], "night_rub": 8000, "visa": "штамп в аэропорту",
     "hours_from_moscow": 5.0, "train_hours_from_moscow": None, "airport_km": 18, "station_km": None,
     "climate": {1: (22, 22), 2: (23, 21), 3: (26, 22), 4: (30, 23), 5: (34, 25), 6: (37, 27),
                 7: (38, 28), 8: (38, 28), 9: (36, 28), 10: (32, 27), 11: (27, 25), 12: (23, 23)}},
    {"id": "dubai", "name": "Дубай", "country": "ОАЭ", "aliases": ["дубай", "оаэ", "эмираты"],
     "tags": ["sea", "city", "luxury"], "night_rub": 14000, "visa": "не нужна",
     "hours_from_moscow": 5.3, "train_hours_from_moscow": None, "airport_km": 25, "station_km": None,
     "climate": {1: (24, 22), 2: (25, 21), 3: (28, 23), 4: (33, 25), 5: (38, 28), 6: (40, 31),
                 7: (41, 32), 8: (41, 33), 9: (39, 32), 10: (35, 30), 11: (30, 27), 12: (26, 24)}},
    {"id": "istanbul", "name": "Стамбул", "country": "Турция", "aliases": ["стамбул"],
     "tags": ["city", "culture", "food"], "night_rub": 8500, "visa": "не нужна (до 60 дней)",
     "hours_from_moscow": 3.5, "train_hours_from_moscow": None, "airport_km": 40, "station_km": None,
     "climate": {1: (9, 9), 2: (9, 8), 3: (12, 9), 4: (17, 12), 5: (22, 17), 6: (27, 22),
                 7: (29, 24), 8: (29, 25), 9: (25, 23), 10: (20, 19), 11: (15, 15), 12: (11, 11)}},
    {"id": "baku", "name": "Баку", "country": "Азербайджан", "aliases": ["баку", "азербайджан"],
     "tags": ["city", "food", "sea"], "night_rub": 6000, "visa": "не нужна",
     "hours_from_moscow": 3.0, "train_hours_from_moscow": None, "airport_km": 25, "station_km": None,
     "climate": {1: (7, 7), 2: (7, 6), 3: (10, 8), 4: (16, 12), 5: (22, 18), 6: (27, 23),
                 7: (31, 26), 8: (30, 26), 9: (26, 23), 10: (19, 18), 11: (13, 13), 12: (9, 9)}},
    {"id": "phuket", "name": "Пхукет", "country": "Таиланд", "aliases": ["пхукет", "таиланд", "тайланд"],
     "tags": ["sea", "nature"], "night_rub": 7500, "visa": "не нужна (до 30 дней)",
     "hours_from_moscow": 9.5, "train_hours_from_moscow": None, "airport_km": 32, "station_km": None,
     "climate": {1: (32, 28), 2: (33, 28), 3: (33, 29), 4: (34, 30), 5: (32, 30), 6: (31, 29),
                 7: (31, 29), 8: (31, 29), 9: (30, 29), 10: (30, 29), 11: (31, 29), 12: (31, 28)}},
    # Горнолыжные курорты: ski_months — месяцы катания; время перелёта — до ближайшего аэропорта
    {"id": "krasnaya_polyana", "name": "Красная Поляна", "country": "Россия",
     "aliases": ["красная поляна", "полян", "роза хутор", "розу хутор", "газпром лаура"],
     "tags": ["ski", "nature"], "night_rub": 9000, "visa": "не нужна", "ski_months": [12, 1, 2, 3, 4],
     "hours_from_moscow": 2.5, "train_hours_from_moscow": None, "airport_km": 50, "station_km": None,
     "climate": {1: (-3, None), 2: (-2, None), 3: (2, None), 4: (8, None), 5: (13, None), 6: (17, None),
                 7: (20, None), 8: (20, None), 9: (15, None), 10: (9, None), 11: (3, None), 12: (-1, None)}},
    {"id": "sheregesh", "name": "Шерегеш", "country": "Россия", "aliases": ["шерегеш"],
     "tags": ["ski", "nature"], "night_rub": 6000, "visa": "не нужна", "ski_months": [11, 12, 1, 2, 3, 4],
     "hours_from_moscow": 4.3, "train_hours_from_moscow": None, "airport_km": 160, "station_km": None,
     "climate": {1: (-15, None), 2: (-12, None), 3: (-5, None), 4: (3, None), 5: (12, None), 6: (19, None),
                 7: (21, None), 8: (18, None), 9: (11, None), 10: (3, None), 11: (-7, None), 12: (-13, None)}},
    {"id": "dombai", "name": "Домбай", "country": "Россия", "aliases": ["домбай"],
     "tags": ["ski", "nature"], "night_rub": 5500, "visa": "не нужна", "ski_months": [12, 1, 2, 3, 4],
     "hours_from_moscow": 2.3, "train_hours_from_moscow": None, "airport_km": 200, "station_km": None,
     "climate": {1: (-3, None), 2: (-2, None), 3: (2, None), 4: (8, None), 5: (13, None), 6: (17, None),
                 7: (20, None), 8: (20, None), 9: (15, None), 10: (9, None), 11: (3, None), 12: (-1, None)}},
]

# Смещение времени перелёта относительно Москвы (часы) для других городов вылета
ORIGIN_OFFSET = {"Москва": 0.0, "Санкт-Петербург": 0.4, "Казань": 0.3, "Екатеринбург": 1.0,
                 "Новосибирск": 3.0, "Нижний Новгород": 0.3, "Самара": 0.4, "Ростов-на-Дону": 0.0,
                 "Краснодар": 0.0, "Воронеж": 0.1, "Уфа": 0.6, "Пермь": 0.7, "Челябинск": 1.0,
                 "Тюмень": 1.4, "Омск": 2.3, "Томск": 3.2, "Красноярск": 3.5, "Иркутск": 4.5,
                 "Хабаровск": 6.0, "Владивосток": 7.0, "Калининград": 0.8, "Минск": 0.3}
# Ключ — основа слова, ищется подстрокой: «томск» раньше «омск», иначе Томск станет Омском
ORIGIN_ALIASES = {"москв": "Москва", "петербург": "Санкт-Петербург", "питер": "Санкт-Петербург",
                  "спб": "Санкт-Петербург", "казан": "Казань", "екатеринбург": "Екатеринбург",
                  "новосибирск": "Новосибирск", "нижн": "Нижний Новгород", "самар": "Самара",
                  "ростов": "Ростов-на-Дону", "краснодар": "Краснодар", "воронеж": "Воронеж",
                  "уфы": "Уфа", "уфа": "Уфа", "перм": "Пермь", "челябинск": "Челябинск",
                  "тюмен": "Тюмень", "томск": "Томск", "омск": "Омск", "красноярск": "Красноярск",
                  "иркутск": "Иркутск", "хабаровск": "Хабаровск", "владивосток": "Владивосток",
                  "калининград": "Калининград", "минск": "Минск"}

HOTEL_WORDS = ["Бриз", "Лазурь", "Панорама", "Ривьера", "Магнолия", "Альтаир", "Кипарис",
               "Маяк", "Солнечный берег", "Гранд", "Каскад", "Олива", "Мираж", "Атлас"]
MEALS = ["RO", "BB", "HB", "AI"]   # без питания, завтрак, полупансион, всё включено


def _rng(*parts) -> random.Random:
    seed = int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)
    return random.Random(seed)


def get_destination(dest_id: str) -> dict | None:
    return next((d for d in DESTINATIONS if d["id"] == dest_id), None)


def _stem(alias: str) -> str:
    """Отрезаем окончание, чтобы находить косвенные падежи: «Анталью», «Турцию», «Казани»."""
    return alias[:-1] if len(alias) > 4 and alias[-1] in "аяьйиыеоу" else alias


def find_destination_by_text(text: str) -> dict | None:
    t = text.lower().replace("ё", "е")
    for d in DESTINATIONS:
        if any(_stem(a) in t for a in d["aliases"]):
            return d
    return None


def normalize_origin(text: str) -> str | None:
    t = text.lower()
    for k, v in ORIGIN_ALIASES.items():
        if k in t:
            return v
    return None


# ---------------------------------------------------------------------------
# Инструменты
# ---------------------------------------------------------------------------
def search_destinations(origin: str, month: int, trip_type: list[str],
                        max_travel_hours: float | None, budget_per_night: int | None,
                        travelers: int, nights: int) -> list[dict]:
    """Направления с погодой, ценой и временем в пути. Не фильтрует жёстко —
    возвращает всё с пометками, ранжирует planner."""
    out = []
    for d in DESTINATIONS:
        if d["name"] == origin:
            continue
        air, water = d["climate"][month]
        hours = d["hours_from_moscow"] + ORIGIN_OFFSET.get(origin, 1.0)
        flight_rub = int(4500 * hours * travelers)
        stay_rub = d["night_rub"] * nights * max(1, (travelers + 1) // 2)
        out.append({
            "id": d["id"], "name": d["name"], "country": d["country"], "tags": d["tags"],
            "visa": d["visa"], "air_temp": air, "water_temp": water,
            "flight_hours": round(hours, 1),
            "train_hours": d["train_hours_from_moscow"] if origin == "Москва" else None,
            "est_total_rub": flight_rub * 2 + stay_rub,
            "est_night_rub": d["night_rub"],
            "ski_season": month in d.get("ski_months", []),
            "data_source": "mock",
        })
    return out


def get_weather(dest_id: str, month: int) -> dict:
    d = get_destination(dest_id)
    air, water = d["climate"][month]
    return {"dest_id": dest_id, "month": month, "air_temp": air, "water_temp": water,
            "data_source": "mock (климатическая норма)"}


def search_transport(origin: str, dest_id: str, day: date, travelers: int,
                     direction: str = "out") -> list[dict]:
    """Рейсы и поезда на дату. direction: out | back."""
    d = get_destination(dest_id)
    rng = _rng(origin, dest_id, day.isoformat(), direction)
    src, dst = (origin, d["name"]) if direction == "out" else (d["name"], origin)
    base_hours = d["hours_from_moscow"] + ORIGIN_OFFSET.get(origin, 1.0)
    options = []
    for i in range(rng.randint(3, 5)):
        stops = 0 if (i < 2 or rng.random() < 0.4) else 1
        dur = base_hours + stops * rng.uniform(2.0, 5.0)
        dep = datetime.combine(day, datetime.min.time()) + timedelta(
            hours=rng.choice([1, 6, 7, 9, 11, 13, 15, 18, 21, 23]), minutes=rng.choice([0, 15, 30, 45]))
        arr = dep + timedelta(hours=dur)
        price = int((3500 * base_hours + rng.randint(-2000, 6000)) * (0.8 if stops else 1.0))
        options.append({
            "id": f"fl-{direction}-{dest_id}-{day.isoformat()}-{i}", "mode": "plane",
            "carrier": rng.choice(["Аэрофлот", "S7", "Победа", "Уральские авиалинии", "Turkish Airlines"]),
            "from": src, "to": dst, "departure": dep.isoformat(timespec="minutes"),
            "arrival": arr.isoformat(timespec="minutes"), "duration_h": round(dur, 1), "stops": stops,
            "baggage": rng.random() > 0.35, "price_rub": max(3000, price) * travelers,
            "data_source": "mock"})
    if d["train_hours_from_moscow"] and origin == "Москва":
        dur = d["train_hours_from_moscow"]
        dep = datetime.combine(day, datetime.min.time()) + timedelta(hours=rng.choice([0, 8, 13, 20]))
        options.append({
            "id": f"tr-{direction}-{dest_id}-{day.isoformat()}", "mode": "train", "carrier": "РЖД",
            "from": src, "to": dst, "departure": dep.isoformat(timespec="minutes"),
            "arrival": (dep + timedelta(hours=dur)).isoformat(timespec="minutes"),
            "duration_h": dur, "stops": 0, "baggage": True,
            "price_rub": int(900 * dur ** 0.8 + 1500) * travelers, "data_source": "mock"})
    return options


def search_hotels(dest_id: str, check_in: date, check_out: date, adults: int,
                  children: int, pets: int) -> list[dict]:
    d = get_destination(dest_id)
    nights = (check_out - check_in).days
    season = 1.3 if check_in.month in (6, 7, 8, 12) else 1.0
    out = []
    for i in range(14):
        rng = _rng("hotel", dest_id, i)          # отели стабильны между запросами
        stars = rng.choice([2, 3, 3, 4, 4, 4, 5, 5])
        night = int(d["night_rub"] * (0.45 + 0.28 * stars) * season * rng.uniform(0.85, 1.2))
        meal = rng.choice(MEALS if "all_inclusive" in d["tags"] else MEALS[:3])
        if meal == "AI":
            night = int(night * 1.6)
        max_guests = rng.choice([2, 3, 4, 4, 5])
        sea = "sea" in d["tags"]
        rating = round(min(9.7, max(6.2, 6.0 + stars * 0.6 + rng.uniform(-0.8, 0.9))), 1)
        out.append({
            "id": f"h-{dest_id}-{i}", "name": f"{HOTEL_WORDS[i % len(HOTEL_WORDS)]} {stars}*",
            "dest_id": dest_id, "stars": stars, "price_per_night_rub": night,
            "price_total_rub": night * nights, "nights": nights, "meals": meal,
            "free_cancellation": rng.random() > 0.4,
            "cancellation_deadline_days": rng.choice([1, 3, 7]),
            "max_guests": max_guests, "family_rooms": max_guests >= 4 or rng.random() > 0.6,
            "pets_allowed": rng.random() > 0.75, "kids_club": stars >= 4 and rng.random() > 0.5,
            "beach_m": rng.choice([50, 150, 300, 600, 1200]) if sea else None,
            "center_km": round(rng.uniform(0.3, 9), 1), "airport_km": d["airport_km"] + rng.randint(-8, 10),
            "check_in": rng.choice(["14:00", "15:00"]), "check_out": "12:00",
            "early_checkin": rng.random() > 0.5,
            "rating": rating, "reviews": rng.randint(12, 2400),
            "review_pros": rng.sample(["чисто", "вкусные завтраки", "тихо", "близко к морю",
                                       "приветливый персонал", "хорошо для детей", "вид из окна"], 2),
            "review_cons": rng.sample(["шумно вечером", "маленькие номера", "далеко от центра",
                                       "медленный Wi-Fi", "старый ремонт"], 1),
            "data_source": "mock",
            "fits_guests": max_guests >= adults + children,
        })
    return out


def search_transfers(dest_id: str, from_point: str, to_point: str, when: str,
                     travelers: int, pets: int) -> list[dict]:
    """from_point / to_point: airport | station | hotel:<id>."""
    d = get_destination(dest_id)
    km = d["airport_km"] if "airport" in (from_point + to_point) else (d["station_km"] or 5)
    opts = [
        {"id": f"tf-{dest_id}-{from_point}-{to_point}-taxi", "kind": "taxi", "minutes": int(km * 1.5 + 10),
         "price_rub": int(30 * km + 400) * (1 if travelers <= 4 else 2), "pets_ok": True},
        {"id": f"tf-{dest_id}-{from_point}-{to_point}-shuttle", "kind": "shuttle", "minutes": int(km * 1.5 + 45),
         "price_rub": 700 * travelers, "pets_ok": False},
        {"id": f"tf-{dest_id}-{from_point}-{to_point}-public", "kind": "public", "minutes": int(km * 3 + 20),
         "price_rub": 120 * travelers, "pets_ok": False},
    ]
    for o in opts:
        o.update({"from": from_point, "to": to_point, "when": when, "data_source": "mock"})
    return [o for o in opts if o["pets_ok"] or not pets]


def request_early_checkin(hotel_id: str, check_in: date, arrival: str) -> dict:
    """Запрос раннего заезда (необязательная функция контракта). Детерминированный мок-ответ отеля."""
    rng = _rng("early", hotel_id, check_in.isoformat())
    return {"status": "confirmed" if rng.random() > 0.4 else "on_request", "data_source": "mock"}


def checkout(cart: dict) -> dict:
    """Имитация оплаты. Всегда успешна."""
    rng = _rng("order", cart.get("total_rub"), cart.get("hotel_id"))
    return {"order_id": f"OST-{rng.randint(100000, 999999)}", "status": "paid",
            "paid_rub": cart["total_rub"], "payment": "имитация", "created_at": datetime.now().isoformat(timespec="seconds")}
