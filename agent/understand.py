"""Понимание запроса: сообщение -> патч -> TripState.

1. rule_patch()   — быстрый разбор правилами (работает всегда, без LLM).
2. LLM.extract_patch() — разбор моделью; её поля перекрывают правила.
3. apply_patch()  — применяет патч, возвращает множество изменённых полей.
4. infer_hidden() — скрытые требования, каждое записывается как допущение.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from agent.state import TripState
from tools import get_backend

MONTHS = [(r"январ", 1), (r"феврал", 2), (r"март", 3), (r"апрел", 4), (r"\bма[йяе]\b", 5),
          (r"июн", 6), (r"июл", 7), (r"август", 8), (r"сентябр", 9), (r"октябр", 10),
          (r"ноябр", 11), (r"декабр", 12)]
NUM_WORDS = {"один": 1, "одна": 1, "день": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
             "пять": 5, "шесть": 6, "семь": 7, "неделю": 7, "десять": 10}
ORDINALS = {"перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5}
_NUM = r"(\d+|один|одна|два|две|три|четыре|пять|шесть|семь|десять)"


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else NUM_WORDS.get(tok, 1)


def rule_patch(message: str, state: TripState) -> dict:
    t = message.lower().replace("ё", "е")
    be = get_backend()
    p: dict = {}

    # --- куда и откуда --------------------------------------------------
    m = re.search(r"\bиз\s+([а-я\-]+(?:\s+[а-я]+)?)", t)
    if m and (origin := be.normalize_origin(m.group(1))):
        p["origin_city"] = origin
    dest = be.find_destination_by_text(re.sub(r"\bиз\s+[а-я\-]+", "", t))
    if dest:
        p["destination_name"] = dest["name"]
    types = []
    if re.search(r"мор|пляж|тепл|позагора|купат", t): types.append("sea")
    if re.search(r"город|экскурс|музе|прогул", t): types.append("city")
    if re.search(r"\bгор[ыа]|природ|поход", t): types.append("nature")
    if re.search(r"лыж|сноуборд|горнолыж", t): types.append("ski")
    if re.search(r"гастро|поесть|кухн", t): types.append("food")
    if types: p["trip_type"] = types
    m = re.search(r"\b(к|на|в)\s+(тепл\w*\s+)?(мор\w*|пляж\w*|гор\w*)", t)
    if m: p["destination_hint"] = m.group(0)

    # --- когда ----------------------------------------------------------
    for pat, num in MONTHS:
        if re.search(pat, t):
            p["month"] = num
            break
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
    ages = [int(a) for a in re.findall(r"(\d{1,2})\s*(?:лет|год|года)", t) if int(a) < 18]
    if re.search(r"с реб|с сын|с доч|ребенк", t):
        p["children_ages"] = ages[:1] or [7]
    if re.search(r"с детьми|дет(ей|и)\b", t):
        m = re.search(r"(\d+|двое|трое)\s*дет", t)
        n = {"двое": 2, "трое": 3}.get(m.group(1), None) if m else None
        n = n or (int(m.group(1)) if m and m.group(1).isdigit() else max(2, len(ages)))
        p["children_ages"] = (ages + [7] * n)[:n]
    if re.search(r"с собак|с пс[оа]|с кот|с кошк|с питомц|собака", t): p["pets"] = 1

    # --- бюджет -----------------------------------------------------------
    m = re.search(r"(\d[\d\s]*[\d])\s*(тыс|т\.р|k\b|к\b)|(\d+)\s*(тыс|к\b)", t)
    if m:
        p["budget_total_rub"] = int(re.sub(r"\s", "", m.group(1) or m.group(3))) * 1000
    else:
        m = re.search(r"(\d{2,3}[\s ]?\d{3})\s*(р|₽|руб)", t) or re.search(r"бюджет\D{0,10}(\d{5,7})", t)
        if m: p["budget_total_rub"] = int(re.sub(r"\D", "", m.group(1)))
    if re.search(r"недорог|бюджетн|дешев|эконом", t) and "budget_total_rub" not in p:
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
    if re.search(r"оформ|бронир|оплат|покупа|\bбер(у|ем)\b все|готов купить", t):
        p["intent"] = "checkout"
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


def merge_patches(rules: dict, llm: dict | None) -> dict:
    if not llm:
        return rules
    merged = {**rules}
    for k, v in llm.items():
        if v not in (None, "", [], {}):
            merged[k] = v
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
        _confirm(state, "origin")
    if p.get("destination_hint"):
        state.destination_hint = p["destination_hint"]
    if p.get("trip_type"):
        new = sorted(set(state.trip_type) | set(p["trip_type"]))
        setf("trip_type", state, "trip_type", new)
    if p.get("destination_name"):
        d = be.find_destination_by_text(p["destination_name"])
        if d:
            setf("destination", state.selection, "destination_id", d["id"])
            if "destination_id" not in state.selection.locked:
                state.selection.locked.append("destination_id")

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
        state.drop_assumption("adults")
    if p.get("children_ages") is not None and "children_ages" in p:
        setf("travelers", tr, "children_ages", list(p["children_ages"]))
        _confirm(state, "travelers")
    if p.get("pets") is not None and "pets" in p:
        setf("travelers", tr, "pets", p["pets"])

    # бюджет
    if p.get("budget_total_rub"):
        setf("budget", state.budget, "total_rub", int(p["budget_total_rub"]))
        state.drop_assumption("budget")
    if p.get("budget_level"):
        setf("budget", state.budget, "level", p["budget_level"])
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
