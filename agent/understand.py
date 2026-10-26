"""Понимание запроса: сообщение -> патч -> TripState.

1. rule_patch()   — быстрый разбор правилами (работает всегда, без LLM).
2. LLM.extract_patch() — разбор моделью; её поля перекрывают правила.
3. apply_patch()  — применяет патч, возвращает множество изменённых полей.
4. infer_hidden() — скрытые требования, каждое записывается как допущение.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from agent.normalize import english_to_russian, fix_typos
from agent.places import lookup_place, tags_label
from agent.state import TripState
from tools import get_backend

MONTHS = [(r"январ", 1), (r"феврал", 2), (r"март", 3), (r"апрел", 4), (r"\bма[йяе]\b", 5),
          (r"июн", 6), (r"июл", 7), (r"август", 8), (r"сентябр", 9), (r"октябр", 10),
          (r"ноябр", 11), (r"декабр", 12)]
NUM_WORDS = {"один": 1, "одна": 1, "день": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
             "пять": 5, "шесть": 6, "семь": 7, "неделю": 7, "десять": 10}
ORDINALS = {"перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5}
_NUM = r"(\d+|один|одна|два|две|три|четыре|пять|шесть|семь|десять)"
KIDS_COUNT = {"двое": 2, "двоих": 2, "двумя": 2, "двух": 2, "трое": 3, "троих": 3, "тремя": 3,
              "трех": 3, "четверо": 4, "четырьмя": 4}
_KIDS_NUM = r"(\d+|" + "|".join(KIDS_COUNT) + r")"
GROUP_COUNT = {"двое": 2, "трое": 3, "четверо": 4, "пятеро": 5, "шестеро": 6}
MONTH_IN = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе",
            "сентябре", "октябре", "ноябре", "декабре"]
MONTH_STEMS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7,
               "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
_MONTH_GEN = r"(январ|феврал|март|апрел|ма(?=[яй])|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*"
# «на Новый год», «в Майские» — заглавная буква, но не место
NOT_PLACES = ("новый", "новог", "рождеств", "майск", "выходн", "январ", "феврал", "март", "апрел",
              "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
PREFER_LABEL = {"price": "цена", "travel_time": "время в пути", "location": "расположение",
                "rating": "рейтинг отеля", "cancellation": "бесплатная отмена", "meals": "питание"}
# (фраза, какой вес поднять, от чего мерить расположение)
PREFER = [(r"ближе к (мор|пляж)\w*|рядом с (мор|пляж)\w*|у моря|первая линия|первой линии", "location", "beach"),
          (r"ближе к центру|в центре|рядом с центром", "location", "center"),
          (r"рейтинг\w* (повыше|получше|выше)|получше отел|отел\w* получше|хорош\w* отзыв", "rating", None)]


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else NUM_WORDS.get(tok, 1)


def _future_date(day: int, month: int, today: date) -> date | None:
    """Ближайшая будущая дата с таким днём и месяцем: «5 марта» в октябре — это март следующего года."""
    try:
        d = date(today.year, month, day)
        return d if d >= today else date(today.year + 1, month, day)
    except ValueError:            # «31 февраля»
        return None


def _date_note(t: str, today: date) -> str | None:
    """Названная дата, которую нельзя взять как есть: прошедшая («5 января 2020») или несуществующая («30 февраля»)."""
    m = re.search(rf"\b(\d{{1,2}})\s+{_MONTH_GEN}\s+(20\d\d)", t)
    if m:
        try:
            asked = date(int(m.group(3)), MONTH_STEMS[m.group(2)], int(m.group(1)))
        except ValueError:
            asked = None
        future = _future_date(int(m.group(1)), MONTH_STEMS[m.group(2)], today)
        if asked and future and asked < today:
            return f"{m.group(0)} уже прошло — беру ближайшее {future:%d.%m.%Y}."
    for m in re.finditer(rf"\b(\d{{1,2}})\s+{_MONTH_GEN}", t):
        if _future_date(int(m.group(1)), MONTH_STEMS[m.group(2)], today) is None:
            return f"{m.group(0)} не бывает — подбираю даты в {MONTH_IN[MONTH_STEMS[m.group(2)] - 1]}."
    return None


def _parse_dates(t: str, today: date) -> tuple[date | None, date | None]:
    """«5 марта», «с 14 по 21 ноября», «с 28 декабря по 3 января»."""
    m = re.search(rf"\b(\d{{1,2}})(?:\s+{_MONTH_GEN})?\s*(?:по|до|-|–|—)\s*(\d{{1,2}})\s+{_MONTH_GEN}", t)
    if m:
        m2 = MONTH_STEMS[m.group(4)]
        start = _future_date(int(m.group(1)), MONTH_STEMS[m.group(2)] if m.group(2) else m2, today)
        if not start:
            return None, None
        try:
            end = date(start.year, m2, int(m.group(3)))
        except ValueError:
            return start, None
        if end <= start:
            end = date(end.year + 1, end.month, end.day)
        return start, end
    m = re.search(rf"\b(\d{{1,2}})\s+{_MONTH_GEN}", t)
    if m:
        return _future_date(int(m.group(1)), MONTH_STEMS[m.group(2)], today), None
    return None, None


def rule_patch(message: str, state: TripState, today: date | None = None) -> dict:
    today = today or date.today()
    t, typos = fix_typos(english_to_russian(message.lower().replace("ё", "е")))
    be = get_backend()
    p: dict = {"typo_fixes": typos} if typos else {}

    # --- куда и откуда --------------------------------------------------
    m = re.search(r"\bиз\s+([а-я\-]+(?:\s+[а-я]+)?)", t)
    if m and (origin := be.normalize_origin(m.group(1))):
        p["origin_city"] = origin
    elif m := re.search(r"(?i:\bиз)\s+([А-ЯЁ][а-яё\-]+)", message):
        p["unknown_origin"] = m.group(1)          # город с заглавной, но мы его не знаем — скажем честно
    dest = be.find_destination_by_text(re.sub(r"\bиз\s+[а-я\-]+", "", t))
    place = None if dest else lookup_place(re.sub(r"\bиз\s+[а-я\-]+", "", t))
    if dest:
        p["destination_name"] = dest["name"]
    elif place:
        p["destination_name"] = place["name"]      # известное место, которого нет в каталоге — скажем честно
    else:
        for m in re.finditer(r"(?i:\b(?:в|во|на))\s+([А-ЯЁ][а-яёА-ЯЁ\-]+)", message):
            if not m.group(1).lower().startswith(NOT_PLACES):
                p["destination_name"] = m.group(1)   # не нашли в справочнике — planner скажет об этом
                break
    for pat, field, near in PREFER:
        if re.search(pat, t):
            p["prefer"] = field
            if near:
                p["near"] = near
            t_types = re.sub(pat, "", t)          # «ближе к морю» — про отель, а не про тип отдыха
            break
    else:
        t_types = t
    types = []
    if re.search(r"мор|пляж|тепл|позагора|купат", t_types): types.append("sea")
    if re.search(r"город|экскурс|музе|прогул", t_types): types.append("city")
    if re.search(r"\bгор[ыа]|природ|поход", t_types): types.append("nature")
    if re.search(r"лыж|сноуборд|горнолыж", t_types): types.append("ski")
    if re.search(r"гастро|поесть|кухн", t_types): types.append("food")
    if types: p["trip_type"] = types
    m = re.search(r"важнее\s+(лыж|пляж|мор)", t)          # ответ на «лыжи или пляж — что важнее?»
    if m: p["trip_type_only"] = ["ski" if m.group(1) == "лыж" else "sea"]
    m = re.search(r"\b(к|на|в)\s+(тепл\w*\s+)?(мор\w*|пляж\w*|гор\w*)", t_types)
    if m: p["destination_hint"] = m.group(0)

    # --- когда ----------------------------------------------------------
    for pat, num in MONTHS:
        if re.search(pat, t):
            p["month"] = num
            break
    start, end = _parse_dates(t, today)
    note = _date_note(t, today)
    if note:
        p["date_note"] = note
    if start:
        p["start"], p["month"] = start.isoformat(), start.month
    if end:
        p["end"] = end.isoformat()
    m = re.search(rf"на\s+{_NUM}\s*(дн\w*|ноч\w*|сут\w*|день)\b(?!\s*(позже|раньше))", t)
    if m: p["nights"] = _num(m.group(1))
    if re.search(r"на неделю", t): p["nights"] = 7
    if re.search(r"на (две|2) недели", t): p["nights"] = 14
    if re.search(r"на выходные", t): p["nights"] = 2
    m = re.search(rf"на\s+{_NUM}?\s*(день|дня|дней)\s+(позже|раньше)", t)
    if m:
        n = _num(m.group(1)) if m.group(1) else 1
        p["shift_days"] = n if m.group(3) == "позже" else -n

    # --- кто едет ---------------------------------------------------------
    if re.search(r"вдвоем|с жен|с муж|с девушк|с парн|с подруг|с друг", t): p["adults"] = 2
    if re.search(r"втроем", t): p["adults"] = 3
    m = re.search(r"(\d+)\s*взросл", t)
    if m: p["adults"] = int(m.group(1))
    if re.search(r"\b(один|одна|соло)\b", t) and "adults" not in p and "на один" not in t: p["adults"] = 1
    # возрасты списком: «5 лет», «4 и 9 лет», «3, 8 и 12 лет»
    ages = [int(a) for grp in re.findall(r"((?:\d{1,2}\s*(?:,|и)\s*)*\d{1,2})\s*(?:лет|год)", t)
            for a in re.findall(r"\d+", grp) if int(a) < 18]
    m = re.search(rf"\b{_KIDS_NUM}\s+(?:дет|реб)", t)
    if m or re.search(r"\bдет(ей|и|ьми)\b", t):
        n = (int(m.group(1)) if m.group(1).isdigit() else KIDS_COUNT[m.group(1)]) if m else max(2, len(ages))
        p["children_ages"] = (ages + [7] * n)[:n]
    elif re.search(r"с реб|с сын|с доч|ребенк", t):
        p["children_ages"] = ages[:1] or [7]
    if re.search(r"с собак|с пс[оа]|с кот|с кошк|с питомц|собака", t): p["pets"] = 1
    # «нас трое», «нас 5 человек» — всего, вместе с детьми
    m = re.search(r"\bнас\s+(\d{1,2}|" + "|".join(GROUP_COUNT) + r")\b", t)
    if m and "adults" not in p:
        n = int(m.group(1)) if m.group(1).isdigit() else GROUP_COUNT[m.group(1)]
        p["adults"] = max(1, n - len(p.get("children_ages", [])))

    # --- бюджет -----------------------------------------------------------
    m = re.search(r"(\d[\d\s]*[\d])\s*(тыс|т\.р|k\b|к\b)|(\d+)\s*(тыс|к\b)", t)
    if m:
        p["budget_total_rub"] = int(re.sub(r"\s", "", m.group(1) or m.group(3))) * 1000
    else:
        m = re.search(r"(\d{2,3}[\s ]?\d{3})\s*(р|₽|руб)", t) or re.search(r"бюджет\D{0,10}(\d{5,7})", t)
        if m: p["budget_total_rub"] = int(re.sub(r"\D", "", m.group(1)))
    if re.search(r"недорог|бюджетн|дешев|эконом|денег\s+(?:немного|мало|в обрез)|немного денег|поскромнее|сэконом|скромн\w*\s+бюджет", t) and "budget_total_rub" not in p:
        if re.search(r"(бюджет|подешевл|дешевле)\s*(меньше|поменьше)?", t) and state.plan:
            p["budget_factor"] = 0.85
        else:
            p["budget_level"] = "low"
    if re.search(r"бюджет (меньше|поменьше)|урез", t):
        m = re.search(r"на (\d+)\s*%", t)
        p["budget_factor"] = 1 - int(m.group(1)) / 100 if m else 0.8
    if re.search(r"\b(люкс|роскош|премиум)", t): p["budget_level"] = "high"

    # --- ограничения и обязательные условия --------------------------------
    if re.search(r"без пересад|прям(ой|ым)", t): p["direct_only"] = True
    if re.search(r"без долгих перел|недолго лет|недалеко|не далеко", t): p["max_travel_hours"] = 4
    m = re.search(r"не (больше|дольше) (\d+)\s*час", t)
    if m: p["max_travel_hours"] = int(m.group(2))
    if re.search(r"поезд|\bжд\b|на поезде|поездом", t): p["transport"] = "train"
    must = []
    if re.search(r"вс[её] включено|олл|all inclusive|\bai\b", t): must.append("all_inclusive")
    if re.search(r"завтрак", t): must.append("breakfast")
    if re.search(r"бесплатн\w* отмен|с отменой|можно отменить", t): must.append("free_cancellation")
    if must: p["must_have_add"] = must

    # --- намерение ----------------------------------------------------------
    if state.order and re.search(r"ранн\w* (заезд|заселен)|заселиться (пораньше|раньше)|заехать (пораньше|раньше)", t):
        p["intent"] = "early_checkin"
    elif state.order and re.search(r"напомина|напомни|что когда|расписани", t):
        p["intent"] = "reminders"
    elif state.order and re.search(r"трансфер|такси до отеля|добраться до отеля", t):
        p["intent"] = "transfer"
    elif re.search(r"оформ|бронир|оплат|покупа|\bбер(у|ем)\b все|готов купить", t):
        p["intent"] = "checkout"
    elif state.plan and re.search(r"друг(ой|ие|ую) (отел|гостиниц)|(отел\w*|гостиниц\w*) (не нравит|друг)|смени\w* отел", t):
        p.update({"intent": "select", "select_kind": "hotel", "select_other": True})
    else:
        idx = None
        for stem, n in ORDINALS.items():
            if re.search(rf"\b{stem}\w*\b", t):
                idx = n
                break
        if idx is None and re.fullmatch(r"\s*[1-5]\s*", t):
            idx = int(t.strip())
        if idx and re.search(r"выбира|давай|вариант|бер|хочу|подойд|отел|\b[1-5]\b|^\s*\w+\s*$", t):
            p["intent"] = "select"
            p["select_index"] = idx
            if "отел" in t: p["select_kind"] = "hotel"
            elif "рейс" in t or "туда" in t: p["select_kind"] = "outbound"
        elif dest and state.stage == "inspire":
            p["intent"] = "select"
            p["select_kind"] = "destination"
    p.setdefault("intent", "change" if state.plan else "plan")
    return p


# Списки, которые LLM дополняет, а не заменяет: модель часто возвращает только часть типов
UNION_KEYS = ("trip_type", "must_have_add")


def merge_patches(rules: dict, llm: dict | None) -> dict:
    """LLM перекрывает правила, но пустым, False или 0 найденное правилами не затирает:
    модель часто молча пропускает поля или ставит «по умолчанию»."""
    if not llm:
        return rules
    merged = {**rules}
    for k, v in llm.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        if (v is False or v == 0) and rules.get(k):
            continue
        if k == "adults" and v == 1 and "adults" not in rules:
            continue      # модель ставит 1 «по умолчанию» — пусть сработает видимое допущение infer_hidden
        if k == "origin_city" and not get_backend().normalize_origin(v):
            merged.setdefault("unknown_origin", v)       # город, откуда не умеем искать, — скажем честно
            continue
        if k == "destination_name" and not get_backend().find_destination_by_text(v):
            # модель пишет «Antalya» латиницей: переводим как правила, а не нашли — верим правилам (english-family)
            ru = english_to_russian(v.lower())
            if get_backend().find_destination_by_text(ru):
                v = ru
            elif rules.get(k) and get_backend().find_destination_by_text(rules[k]):
                continue
        if k in UNION_KEYS and rules.get(k):
            v = sorted(set(rules[k]) | set(v))
        merged[k] = v
    # «other» от модели не должен выбросить запрос, в котором правила нашли данные о поездке
    if llm.get("intent") == "other" and any(k not in ("intent", "typo_fixes") for k in rules):
        merged["intent"] = rules.get("intent", "plan")
    return merged


def apply_patch(state: TripState, p: dict) -> set[str]:
    """Применить патч. Возвращает изменённые группы полей для пересборки."""
    be = get_backend()
    changed: set[str] = set()

    def setf(group: str, obj, attr: str, value):
        if getattr(obj, attr) != value:
            setattr(obj, attr, value)
            changed.add(group)

    if "origin_city" in p:
        setf("origin_city", state, "origin_city", be.normalize_origin(p["origin_city"]) or p["origin_city"])
        state.drop_assumption("profile_origin")      # пользователь сказал сам — профиль больше не нужен
        _confirm(state, "origin")
    if p.get("destination_hint"):
        state.destination_hint = p["destination_hint"]
    if p.get("trip_type"):
        new = sorted(set(state.trip_type) | set(p["trip_type"]))
        setf("trip_type", state, "trip_type", new)
    if p.get("trip_type_only"):                   # выбрал одно из несовместимого — остальное убираем
        setf("trip_type", state, "trip_type", list(p["trip_type_only"]))
        state.destination_hint = None             # «на пляже» из противоречивого запроса больше не про поездку
        _confirm(state, "trip_priority")
    if p.get("destination_name"):
        d = be.find_destination_by_text(p["destination_name"])
        if d:
            setf("destination", state.selection, "destination_id", d["id"])
            if "destination_id" not in state.selection.locked:
                state.selection.locked.append("destination_id")
            state.like_place = None
        else:
            # данных по месту нет: подбираем похожее — та же страна, регион, тип отдыха (видно как допущение)
            place = lookup_place(p["destination_name"])
            setf("trip_type", state, "like_place", place)
            if place:
                setf("trip_type", state, "trip_type", sorted(set(state.trip_type) | set(place["tags"])))
                state.add_assumption("like_place", f"похожий отдых: {tags_label(place['tags'])}",
                                     f"по направлению «{place['name']}» нет данных")

    # даты
    if p.get("month"):
        setf("dates", state.dates, "month", p["month"])
        if state.dates.start and state.dates.start.month != p["month"]:
            state.dates.start = state.dates.end = None
        _confirm(state, "dates")
    for key in ("start", "end"):
        if p.get(key):
            setf("dates", state.dates, key, date.fromisoformat(p[key]))
            _confirm(state, "dates")
    if p.get("nights"):
        setf("dates", state.dates, "nights", p["nights"])
        if state.dates.start:
            state.dates.end = state.dates.start + timedelta(days=p["nights"])
        state.drop_assumption("nights")
    if p.get("shift_days"):
        rng = state.resolved_dates()
        if rng:
            start, end = rng
            state.dates.start = start + timedelta(days=p["shift_days"])
            state.dates.end = end + timedelta(days=p["shift_days"])
            state.dates.month = state.dates.start.month
            changed.add("dates")
            state.drop_assumption("dates")

    # гости
    tr = state.travelers
    if p.get("adults"):
        setf("travelers", tr, "adults", p["adults"])
        _confirm(state, "travelers")
        _confirm(state, "adults")
        state.drop_assumption("profile_travelers")
        state.drop_assumption("adults")
    if p.get("children_ages") is not None and "children_ages" in p:
        setf("travelers", tr, "children_ages", list(p["children_ages"]))
        state.drop_assumption("profile_travelers")
        _confirm(state, "travelers")
    if p.get("pets") is not None and "pets" in p:
        setf("travelers", tr, "pets", p["pets"])

    # бюджет
    if p.get("budget_total_rub"):
        setf("budget", state.budget, "total_rub", int(p["budget_total_rub"]))
        state.drop_assumption("budget")
    if p.get("budget_level"):
        setf("budget", state.budget, "level", p["budget_level"])
        state.drop_assumption("profile_budget")
    if p.get("budget_factor"):
        base = state.budget.total_rub or (state.plan or {}).get("total_rub")
        if base:
            state.budget.total_rub = int(base * p["budget_factor"])
            changed.add("budget")

    # ограничения
    c = state.constraints
    if p.get("max_travel_hours"):
        setf("constraints", c, "max_travel_hours", float(p["max_travel_hours"]))
        state.drop_assumption("max_travel_hours")
    if "direct_only" in p:
        setf("constraints", c, "direct_only", bool(p["direct_only"]))
    if p.get("transport"):
        setf("constraints", c, "transport", p["transport"])
    for m in p.get("must_have_add", []) or []:
        if m not in state.must_have:
            state.must_have.append(m)
            changed.add("must_have")
    for m in p.get("must_have_remove", []) or []:
        if m in state.must_have:
            state.must_have.remove(m)
            changed.add("must_have")

    # «ближе к морю», «отель получше» — поднять вес критерия, без нового поиска
    k = p.get("prefer")
    if k in PREFER_LABEL:
        setf("weights", state.weights, k, round(max(getattr(state.weights, k) + 0.25, 0.4), 2))
        state.add_assumption("weights", f"в приоритете: {PREFER_LABEL[k]}", "вы так попросили")
        _confirm(state, "no:weights")             # не перетирать вес правилом «недорого»
    if p.get("near") in ("beach", "center"):
        setf("weights", state, "near", p["near"])
    # «ближе к морю» при готовом плане — значит ближе, чем текущий отель, среди подходящих по остальным условиям
    cur = ((state.plan or {}).get("hotel") or {}).get("beach_m")
    if p.get("near") == "beach" and cur:
        from tools.scoring import hotel_filters      # здесь, чтобы не тянуть скоринг в импорт модуля
        if any((h.get("beach_m") or cur) < cur and not hotel_filters(h, state)
               for h in state.candidates.get("hotels", [])):
            setf("location", state.constraints, "max_beach_m", cur - 1)
        else:
            p["near_unavailable"] = cur              # planner честно скажет, что ближе нет
    return changed


def _confirm(state: TripState, slot: str) -> None:
    if slot not in state.confirmed:
        state.confirmed.append(slot)


def infer_hidden(state: TripState) -> None:
    """Скрытые требования и значения по умолчанию. Всё видно как допущения."""
    tr = state.travelers
    if tr.has_kids:
        if "adults" not in state.confirmed:
            tr.adults = 2
            state.add_assumption("adults", "2 взрослых", "с ребёнком обычно едут двое взрослых", "default")
        if state.constraints.max_travel_hours is None and "no:max_travel_hours" not in state.confirmed:
            state.constraints.max_travel_hours = 5
            state.add_assumption("max_travel_hours", "до 5 ч в дороге", "с ребёнком долгий перелёт тяжело переносится")
        state.add_assumption("kids", "семейный номер, детское меню в приоритете",
                             f"едет ребёнок ({', '.join(map(str, tr.children_ages))} лет)")
    if tr.pets:
        if "pets_allowed" not in state.must_have:
            state.must_have.append("pets_allowed")
        state.add_assumption("pets", "только отели, где можно с животными; трансфер — такси",
                             "с вами едет питомец")
    if state.budget.level == "low" and state.weights.price < 0.45 and "no:weights" not in state.confirmed:
        state.weights.price = 0.45
        state.add_assumption("weights", "цена — главный критерий", "вы просили недорого")
    if state.budget.total_rub is None and state.budget.level is None:
        state.add_assumption("budget", "средний ценовой уровень", "бюджет не назван", "default")
    if not state.dates.nights and not state.dates.end:
        state.add_assumption("nights", "7 ночей", "длительность не названа", "default")
    rng = state.resolved_dates()
    if rng and not state.dates.start:
        state.add_assumption("dates", f"{rng[0]:%d.%m} – {rng[1]:%d.%m}",
                             "назван только месяц — взяли ближайшие выходные после 10-го", "default")
