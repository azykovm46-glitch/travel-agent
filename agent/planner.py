"""Оркестратор агента: одна точка входа handle().

Поток одного хода:
  сообщение/действие -> патч (правила + LLM) -> TripState -> скрытые требования
  -> пересборка (сброс устаревшего) -> следующий шаг по политике
  -> инструменты + скоринг + проверка стыковок -> карточки + объяснения + сообщение.
"""
from __future__ import annotations

import copy
import re
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from agent import policy
from agent.after_booking import build_reminders, request_early_checkin
from agent.explain import (_rub, explain_destination, explain_hotel, explain_transport, MEAL_NAME,
                           unsupported_numbers)
from agent.llm import LLM
from agent.memory import ProfileStore, SessionStore, apply_profile, forget_profile_field, profile_from_booking
from agent.replan import invalidate, plan_diff
from agent.schemas import AgentResponse, Card, QuickReply, TraceStep
from agent.state import TripState, summarize
from agent.understand import apply_patch, infer_hidden, merge_patches, rule_patch
from tools import get_backend
from tools.scoring import hotel_filters, score_destinations, score_hotels, score_transport
from tools.validator import validate_plan


log = logging.getLogger(__name__)

# Штраф комбинации за предупреждение стыковки (по умолчанию 0.03): выезд накануне — лишняя оплаченная ночь
WARNING_PENALTY = {"leave_day_before": 0.12, "early_arrival": 0.03, "late_arrival": 0.03}

# Первое сообщение без этих слов и без данных о поездке — офтоп
TRAVEL_WORDS = (r"отдох|отпуск|поездк|путешеств|съезд|поехать|поеду|едем|лететь|полет|улет|тур\b|билет|отел|"
                r"мор|пляж|гор[ыа]|выходн|куда|trip|travel|vacation|holiday|hotel|flight")

# Что показывать в UI, пока шаг выполняется (стриминг по WebSocket)
STEP_LABELS = {
    "understand": "разбираю запрос…", "search_destinations": "подбираю направления…",
    "search_transport": "ищу рейсы и поезда…", "search_hotels": "ищу отели…", "score": "сравниваю варианты…",
    "assemble": "собираю поездку и проверяю бюджет…", "validate": "проверяю стыковки…",
    "write": "формулирую ответ…", "checkout": "оформляю бронь…",
}


class Tracer:
    """Шаги агента для панели «Как агент думает». on_event — колбэк для стриминга:
    {"type": "step_start", ...} в начале шага и {"type": "trace", ...} по его завершении."""

    def __init__(self, on_event=None) -> None:
        self.steps: list[TraceStep] = []
        self.on_event = on_event

    def _emit(self, event: dict) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as e:      # упавший слушатель не должен ронять агента
                log.warning("trace listener failed: %s", e)

    def _record(self, s: TraceStep) -> None:
        self.steps.append(s)
        self._emit({"type": "trace", **s.model_dump()})

    @contextmanager
    def step(self, name: str, detail: str = ""):
        self._emit({"type": "step_start", "step": name, "label": STEP_LABELS.get(name, name + "…")})
        t0 = time.perf_counter()
        holder = {"detail": detail}
        yield holder
        self._record(TraceStep(step=name, detail=holder["detail"], ms=int((time.perf_counter() - t0) * 1000)))

    def add(self, name: str, detail: str) -> None:
        self._record(TraceStep(step=name, detail=detail))


def describe_request(s: TripState) -> str:
    parts = []
    if s.destination_hint and not s.selection.destination_id:
        parts.append(s.destination_hint)
    if s.origin_city:
        parts.append(f"из города {s.origin_city}")
    rng = s.resolved_dates()
    if rng:
        parts.append(f"{rng[0]:%d.%m}–{rng[1]:%d.%m}")
    t = s.travelers
    if "travelers" in s.confirmed or t.children_ages or t.pets:   # не выдаём умолчание за понятое
        who = "1 взрослый" if t.adults == 1 else f"{t.adults} взрослых"
        if t.children_ages:
            who += " + дети " + ", ".join(f"{a} лет" for a in t.children_ages)
        if t.pets:
            who += " + питомец"
        parts.append(who)
    if s.budget.total_rub:
        parts.append(f"бюджет до {_rub(s.budget.total_rub)}")
    elif s.budget.level == "low":
        parts.append("недорого")
    return ", ".join(parts)


class TravelAgent:
    def __init__(self, llm: LLM | None = None, today: date | None = None,
                 memory: ProfileStore | None = None, sessions: SessionStore | None = None) -> None:
        self.llm = llm or LLM()
        self.today = today
        self.memory = memory                  # None — без памяти между сессиями (тесты, evals)
        self.sessions = sessions or SessionStore()   # без пути — только в памяти

    # ------------------------------------------------------------------ API
    def get_state(self, session_id: str) -> TripState | None:
        return self.sessions.get(session_id)

    def handle(self, session_id: str | None = None, message: str | None = None,
               action: dict | None = None, on_event=None, user_id: str | None = None) -> AgentResponse:
        """on_event — необязательный колбэк для стриминга шагов (см. Tracer).
        user_id — чей профиль подставить в новую сессию и обновить после брони."""
        sid = session_id or uuid.uuid4().hex[:12]
        state = self.sessions.get(sid) or TripState()
        tr = Tracer(on_event)
        today = self.today or date.today()
        first_turn = state.stage == "new"
        if user_id and first_turn and not state.user_id:
            state.user_id = user_id
            profile = self.memory.get(user_id) if self.memory else None
            if profile:
                apply_profile(state, profile, today)
                tr.add("profile", "подставил профиль: " + ", ".join(
                    a.value for a in state.assumptions if a.source == "profile"))
        old_plan = copy.deepcopy(state.plan)

        patch: dict = {}
        if message:
            with tr.step("understand") as h:
                rules = rule_patch(message, state, today)
                llm_patch = self.llm.extract_patch(message, summarize(state), today.isoformat())
                patch = merge_patches(rules, llm_patch)
                if llm_patch:
                    source = "LLM + правила"
                elif getattr(self.llm, "enabled", False):
                    source = f"правила (LLM не ответила: {getattr(self.llm, 'last_error', None) or 'пустой ответ'})"
                else:
                    source = "правила (LLM выключена)"
                h["detail"] = source + f": {', '.join(k for k in patch if k not in ('intent', 'typo_fixes')) or 'без новых данных'}" \
                    + (f"; исправил опечатки: {', '.join(rules['typo_fixes'])}" if rules.get("typo_fixes") else "")
        extra_changed: set[str] = set()
        if action:
            patch, extra_changed = self._action_to_patch(action, state, patch)

        intent = patch.get("intent", "plan")
        changed = apply_patch(state, patch) | extra_changed
        infer_hidden(state)

        resp = self._route(sid, state, tr, intent, patch, changed, old_plan, today)
        notes = self._notes(patch, state, resp.stage, message if first_turn else None)
        if notes:
            resp.message = " ".join(notes + [resp.message])
        self.sessions.save(sid, state)
        return resp

    def _notes(self, patch: dict, state: TripState, stage: str, first_message: str | None) -> list[str]:
        """Честно говорим, что из запроса не смогли учесть, вместо молчаливой подмены."""
        be, notes = get_backend(), []
        if first_message and not any(k != "intent" for k in patch) \
                and not re.search(TRAVEL_WORDS, first_message.lower()):
            notes.append("Я помогаю спланировать поездку: подберу направление, билеты, отель и трансфер. "
                         "С этим вопросом не подскажу, а поездку давайте соберём.")
        elif patch.get("intent") == "other":           # офтоп посреди разговора распознаёт только LLM
            notes.append("С этим вопросом не подскажу — вернёмся к вашей поездке."
                         if state.plan or state.candidates else
                         "С этим вопросом не подскажу, а поездку давайте соберём.")
        name = patch.get("destination_name")
        if name and not be.find_destination_by_text(name):
            notes.append(f"«{name}» нет среди направлений, с которыми я работаю, — подбираю по остальным условиям.")
        raw = patch.get("unknown_origin")
        if raw and not patch.get("origin_city"):
            notes.append(f"Вылеты «из {raw}» пока не умею искать — "
                         + ("выберите ближайший город из списка." if stage == "clarify"
                            else f"считаю от города {state.origin_city}, поправьте, если нужно."))
        if patch.get("near_unavailable"):
            notes.append(f"Ближе {patch['near_unavailable']} м к морю отелей под ваши условия нет — "
                         "поднял вес расположения среди тех, что есть.")
        return notes

    def _route(self, sid: str, state: TripState, tr: Tracer, intent: str, patch: dict,
               changed: set[str], old_plan: dict | None, today: date) -> AgentResponse:

        if changed and state.stage == "booked":
            state.stage = "plan"        # изменение после покупки = пересборка с показом разницы
            tr.add("post_purchase", "изменение условий после покупки — пересобираю и покажу разницу")
        if changed and (state.plan or state.candidates):
            rep = invalidate(state, changed)
            tr.add("replan", f"изменилось: {', '.join(rep['changed'])}; "
                             f"переищу: {', '.join(rep['dropped']) or 'ничего'}; "
                             f"сохранено: {', '.join(rep['kept']) or '—'}")

        step = policy.next_step(state, intent)
        if step == "select":
            step = self._select(state, patch, tr)

        if step == "clarify":
            return self._clarify(sid, state, tr)
        if step == "checkout":
            return self._checkout(sid, state, tr)
        if step == "booked":
            return self._booked(sid, state, tr)
        if step == "early_checkin":
            return self._early_checkin(sid, state, tr)
        if step == "reminders":
            return self._reminders(sid, state, tr)
        policy.apply_defaults(state, today)
        infer_hidden(state)
        if step == "inspire":
            return self._inspire(sid, state, tr)
        return self._plan(sid, state, tr, old_plan)

    def _write(self, tr: Tracer, facts: dict) -> str | None:
        """Текст от LLM — только если все числа в нём есть в фактах. Иначе None, и берётся шаблон."""
        if not getattr(self.llm, "available", getattr(self.llm, "enabled", False)):
            return None
        with tr.step("write") as h:
            text = self.llm.write(facts)
            bad = unsupported_numbers(text, facts) if text else []
            if bad:
                h["detail"] = f"текст LLM отброшен — чисел {', '.join(bad)} нет в данных, взял шаблон"
                return None
            h["detail"] = "текст от LLM" if text else "LLM не ответила — шаблон"
            return text

    # ----------------------------------------------------------- действия UI
    def _action_to_patch(self, action: dict, state: TripState, patch: dict) -> tuple[dict, set[str]]:
        kind = action.get("type")
        changed: set[str] = set()
        p = dict(patch)
        if kind in ("select_destination", "select_hotel", "select_transport"):
            p.update({"intent": "select", "select_id": action["id"],
                      "select_kind": {"select_destination": "destination", "select_hotel": "hotel"}.get(
                          kind, "outbound" if action.get("direction", "out") == "out" else "return")})
        elif kind == "checkout":
            p["intent"] = "checkout"
        elif kind == "change_destination":          # «другие направления» — назад к подбору
            state.selection.destination_id = None
            if "destination_id" in state.selection.locked:
                state.selection.locked.remove("destination_id")
            changed.add("destination")
            p["intent"] = "plan"
        elif kind in ("request_early_checkin", "reminders"):
            p["intent"] = "early_checkin" if kind == "request_early_checkin" else "reminders"
        elif kind == "set_weights":
            for k, v in action.get("weights", {}).items():
                if hasattr(state.weights, k):
                    setattr(state.weights, k, float(v))
            state.drop_assumption("weights")
            state.confirmed.append("no:weights")
            changed.add("weights")
            p.setdefault("intent", "change")
        elif kind == "remove_assumption":
            field = action["field"]
            changed |= forget_profile_field(state, field) if field.startswith("profile_") else set()
            state.drop_assumption(field)
            state.confirmed.append(f"no:{field}")
            if field == "max_travel_hours":
                state.constraints.max_travel_hours = None
                changed.add("constraints")
            p.setdefault("intent", "change")
        elif kind == "update":
            p.update(action.get("patch", {}))
            p.setdefault("intent", "change")
        return p, changed

    def _select(self, state: TripState, patch: dict, tr: Tracer) -> str:
        kind = patch.get("select_kind") or ("destination" if state.stage == "inspire" else "hotel")
        sel_id = patch.get("select_id")
        idx = patch.get("select_index")
        if kind == "destination":
            if not sel_id and idx:
                top = state.candidates.get("destinations_top", [])
                sel_id = top[idx - 1]["id"] if 0 < idx <= len(top) else None
            if sel_id and sel_id != state.selection.destination_id:
                state.selection.destination_id = sel_id
                invalidate(state, {"destination"})
            if sel_id and "destination_id" not in state.selection.locked:
                state.selection.locked.append("destination_id")
            if state.selection.destination_id:
                tr.add("select", f"направление: {state.selection.destination_id}")
                return "plan"
            return "inspire"
        field, pool = {"hotel": ("hotel_id", "hotels_top"), "outbound": ("outbound_id", "outbound_top"),
                       "return": ("return_id", "return_top")}[kind]
        top = state.candidates.get(pool, [])
        if not sel_id and idx:
            sel_id = top[idx - 1]["id"] if 0 < idx <= len(top) else None
        if not sel_id and patch.get("select_other") and top:
            # «другой отель» — следующий по рейтингу после текущего, по кругу
            ids = [x["id"] for x in top]
            cur = getattr(state.selection, field)
            sel_id = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]
        if sel_id:
            setattr(state.selection, field, sel_id)
            if field not in state.selection.locked:
                state.selection.locked.append(field)
            tr.add("select", f"{kind}: {sel_id}")
        return "plan" if state.selection.destination_id else "inspire"

    # ---------------------------------------------------------------- шаги
    def _clarify(self, sid: str, state: TripState, tr: Tracer) -> AgentResponse:
        qs = policy.build_questions(state, self.today)
        state.clarify_rounds += 1
        state.stage = "clarify"
        tr.add("clarify", f"не хватает: {', '.join(policy.missing_slots(state))}")
        understood = describe_request(state)
        msg = self._write(tr, {"stage": "clarify", "understood": understood,
                              "questions": [q.text for q in qs]}) or \
            (f"Понял: {understood}. " if understood else "") + \
            "Чтобы подобрать варианты, уточню: " + " ".join(q.text for q in qs)
        return AgentResponse(session_id=sid, stage=state.stage, message=msg, questions=qs,
                             cards=[self._assumptions_card(state)], trace=tr.steps, state=state)

    def _inspire(self, sid: str, state: TripState, tr: Tracer) -> AgentResponse:
        be = get_backend()
        start, end = state.resolved_dates(self.today)
        with tr.step("search_destinations") as h:
            if "destinations" not in state.candidates:
                state.candidates["destinations"] = be.search_destinations(
                    origin=state.origin_city, month=start.month, trip_type=state.trip_type,
                    max_travel_hours=state.constraints.max_travel_hours,
                    budget_per_night=None, travelers=state.travelers.total, nights=(end - start).days)
                h["detail"] = f"найдено {len(state.candidates['destinations'])} направлений"
            else:
                h["detail"] = "из кэша"
        ranked = score_destinations(state.candidates["destinations"], state)
        top = ranked[:3]
        state.candidates["destinations_top"] = top
        state.stage = "inspire"
        cards = []
        for i, d in enumerate(top, 1):
            cards.append(Card(type="destination", data={**d, "rank": i, "explanation": explain_destination(d)}))
        cards.append(self._assumptions_card(state))
        qr = [QuickReply(label=f"Выбрать: {d['name']}", action={"type": "select_destination", "id": d["id"]}) for d in top]
        ski_note, ski_month = self._ski_season_note(state, ranked, start, end, tr)
        if ski_month:
            qr.insert(0, QuickReply(label=f"Перенести на {policy.MONTH_NOM[ski_month - 1]}",
                                    action={"type": "update", "patch": {"month": ski_month}}))
        best = top[0]
        msg = self._write(tr, {"stage": "inspire", "request": describe_request(state), "shown_note": ski_note,
                              "options": [{k: d[k] for k in ("name", "country", "air_temp", "water_temp", "flight_hours",
                                                             "est_total_rub", "reasons", "tradeoffs")} for d in top],
                              "assumptions": [a.value for a in state.assumptions]}) or (
            f"Под запрос ({describe_request(state)}) подходят: {', '.join(d['name'] for d in top)}. "
            f"Лучше всего — {best['name']}: {', '.join(best['reasons'][:2]) or 'хороший баланс цены и дороги'}, "
            f"ориентировочно {_rub(best['est_total_rub'])} на всех. Выберите направление — соберу поездку целиком.")
        if ski_note:                                  # не доверяем LLM пересказ: оговорка видна всегда
            msg = f"{ski_note} {msg}"
        return AgentResponse(session_id=sid, stage=state.stage, message=msg, cards=cards,
                             quick_replies=qr, trace=tr.steps, state=state)

    def _ski_season_note(self, state: TripState, ranked: list[dict], start: date, end: date,
                         tr: Tracer) -> tuple[str | None, int | None]:
        """Лыжи без трасс или не в сезон — честно сказать и предложить ближайший месяц катания."""
        if "ski" not in state.trip_type or any(d.get("ski_season") for d in ranked):
            return None, None
        if not any("ski" in d["tags"] for d in ranked):
            note = ("Горнолыжных курортов среди направлений, с которыми я работаю, нет — "
                    + ("показываю ближайшее по духу: горы и природу." if any("nature" in d["tags"] for d in ranked)
                       else "показываю лучшее по остальным условиям."))
            tr.add("ski_season", "в каталоге нет горнолыжных направлений")
            return note, None
        be = get_backend()
        for shift in range(1, 12):
            month = (start.month + shift - 1) % 12 + 1
            open_ = [d["name"] for d in be.search_destinations(
                origin=state.origin_city, month=month, trip_type=state.trip_type,
                max_travel_hours=state.constraints.max_travel_hours, budget_per_night=None,
                travelers=state.travelers.total, nights=(end - start).days) if d.get("ski_season")]
            if open_:
                tr.add("ski_season", f"в {policy.MONTH_IN[start.month - 1]} не сезон; ближайший — "
                                     f"{policy.MONTH_NOM[month - 1]}: {', '.join(open_)}")
                return (f"Кататься в {policy.MONTH_IN[start.month - 1]} не получится: на курортах, с которыми "
                        f"я работаю, не сезон. Ближайший сезон — с {policy.MONTH_GEN[month - 1]} "
                        f"({', '.join(open_)}). Пока показываю горы без лыж — или перенесём поездку?"), month
        return None, None

    def _plan(self, sid: str, state: TripState, tr: Tracer, old_plan: dict | None) -> AgentResponse:
        be = get_backend()
        dest = be.get_destination(state.selection.destination_id)
        start, end = state.resolved_dates(self.today)
        t = state.travelers
        with tr.step("search_transport") as h:
            for key, day, direction in (("outbound", start, "out"), ("return", end, "back")):
                if key not in state.candidates:
                    state.candidates[key] = be.search_transport(state.origin_city, dest["id"], day, t.total, direction)
            h["detail"] = f"туда {len(state.candidates['outbound'])}, обратно {len(state.candidates['return'])} вариантов"
        with tr.step("search_hotels") as h:
            if "hotels" not in state.candidates:
                state.candidates["hotels"] = be.search_hotels(dest["id"], start, end, t.adults,
                                                              len(t.children_ages), t.pets)
            h["detail"] = f"{len(state.candidates['hotels'])} отелей"

        with tr.step("score") as h:
            outs = score_transport(state.candidates["outbound"], state)
            backs = score_transport(state.candidates["return"], state)
            hotels = score_hotels(state.candidates["hotels"], state)
            rejected = len(state.candidates["hotels"]) - len(hotels)
            h["detail"] = f"отелей прошло фильтры {len(hotels)}, отсеяно {rejected}"
        state.candidates["outbound_top"], state.candidates["return_top"] = outs[:3], backs[:3]
        state.candidates["hotels_top"] = hotels[:3]
        if not hotels:
            reasons = {r for hh in state.candidates["hotels"] for r in hotel_filters(hh, state)}
            state.stage = "plan"
            state.plan = None           # старый план под новые условия не подходит — оформлять нечего
            return AgentResponse(session_id=sid, stage=state.stage, trace=tr.steps, state=state,
                                 message="Не нашёл отелей, которые подходят под все условия ("
                                         + "; ".join(sorted(reasons)[:3]) + "). Могу ослабить одно из условий или предложить другое направление.",
                                 quick_replies=[QuickReply(label="Другие направления", action={"type": "change_destination"})])

        # Стыковка закладывается в выбор: рейсы, прилетающие в день заезда, — в приоритете.
        # Ночной приезд до ~05 утра — тоже «в день заезда»: номер держат (как в validate_plan).
        # Если все прилетают позже (дальний перелёт), сдвигаем заезд на день прилёта.
        sel = state.selection
        state.drop_assumption("hotel_dates")
        night_limit = f"{start + timedelta(days=1):%Y-%m-%d}T04:30"
        same_day = [o for o in outs if o["arrival"] <= night_limit]
        if same_day:
            outs = same_day + [o for o in outs if o not in same_day]
        else:
            arrival_day = date.fromisoformat(min(outs, key=lambda o: o["arrival"])["arrival"][:10])
            if arrival_day > start and arrival_day < end:
                start = arrival_day
                state.candidates["hotels"] = be.search_hotels(dest["id"], start, end, t.adults,
                                                              len(t.children_ages), t.pets)
                hotels = score_hotels(state.candidates["hotels"], state)
                state.candidates["hotels_top"] = hotels[:3]
                state.add_assumption("hotel_dates", f"заезд в отель {start:%d.%m}",
                                     "все рейсы прилетают на следующий день — не платим за пустую ночь")
                tr.add("stitch", f"все рейсы прилетают {start:%d.%m} — заезд в отель сдвинут на день прилёта")
        night_limit = f"{start + timedelta(days=1):%Y-%m-%d}T04:30"
        out_pool = [o for o in outs if o["id"] == sel.outbound_id] or \
            ([o for o in outs if o["arrival"] <= night_limit] or outs)[:4]
        back_pool = [o for o in backs if o["id"] == sel.return_id] or backs[:4]
        hotel_locked = next((x for x in hotels if x["id"] == sel.hotel_id), None)
        if sel.hotel_id and not hotel_locked:
            tr.add("replan", "выбранный ранее отель больше не подходит под условия — подобрал другой")
            sel.hotel_id = None
        hotel_pool = [hotel_locked] if hotel_locked else hotels[:8]

        def transfers_for(o, b, hotel):
            point = "station" if o["mode"] == "train" else "airport"
            tin = self._pick_transfer(be.search_transfers(dest["id"], point, f"hotel:{hotel['id']}", o["arrival"], t.total, t.pets), state, o["arrival"])
            point_b = "station" if b["mode"] == "train" else "airport"
            tout = self._pick_transfer(be.search_transfers(dest["id"], f"hotel:{hotel['id']}", point_b, b["departure"], t.total, t.pets), state, b["departure"])
            return tin, tout

        with tr.step("assemble") as h:
            budget = state.budget.total_rub
            combos = []
            for o in out_pool:
                for b in back_pool:
                    for hh in hotel_pool:
                        tin, tout = transfers_for(o, b, hh)
                        total = o["price_rub"] + b["price_rub"] + hh["price_total_rub"] + tin["price_rub"] + tout["price_rub"]
                        score = 0.25 * o["score"] + 0.2 * b["score"] + 0.55 * hh["score"]
                        combos.append((score, total, o, b, hh, tin, tout))
            # Стыковку проверяем на каждой комбинации тем же валидатором: длинный трансфер
            # после позднего рейса может увести приезд на следующий день.
            # Ошибки исключают комбинацию, предупреждения (ждать заселения, выезд накануне) — штрафуют.
            stitched = []
            for c in combos:
                issues = validate_plan(
                    {"check_in": start.isoformat(), "check_out": end.isoformat(), "outbound": c[2], "return": c[3],
                     "hotel": c[4], "transfer_in": c[5], "transfer_out": c[6], "total_rub": c[1]},
                    t.total, t.pets, None)
                if not any(i["level"] == "error" for i in issues):
                    penalty = sum(WARNING_PENALTY.get(i["code"], 0.03) for i in issues if i["level"] == "warning")
                    stitched.append((c[0] - penalty, *c[1:]))
            pool = stitched or combos
            fitting = [c for c in pool if not budget or c[1] <= budget]
            if fitting:
                best = max(fitting, key=lambda c: c[0])
                h["detail"] = f"проверено {len(combos)} комбинаций, без ошибок стыковки {len(stitched)}, в бюджет укладываются {len(fitting)}"
            else:
                best = min(pool, key=lambda c: c[1])
                h["detail"] = f"проверено {len(combos)} комбинаций, без ошибок стыковки {len(stitched)}, в бюджет не влезает ни одна — беру самую дешёвую"
            _, total, out, back, hotel, tin, tout = best
            conflict = None
            if not fitting and state.budget.level == "high":
                conflict = (f"Люкс в бюджет {_rub(budget)} не помещается: даже самый доступный вариант стоит "
                            f"{_rub(total)}. Показываю его; если бюджет можно поднять — соберу люкс.")
            sel.outbound_id, sel.return_id = out["id"], back["id"]
            sel.hotel_id, sel.transfer_in_id, sel.transfer_out_id = hotel["id"], tin["id"], tout["id"]

        plan = {
            "destination": {"id": dest["id"], "name": dest["name"], "country": dest["country"]},
            "check_in": start.isoformat(), "check_out": end.isoformat(), "nights": (end - start).days,
            "travelers": t.model_dump(), "outbound": out, "return": back,
            "hotel": {**hotel, "explanation": explain_hotel(hotel, state, hotels)},
            "transfer_in": tin, "transfer_out": tout, "total_rub": total,
            "price_breakdown": {"transport": out["price_rub"] + back["price_rub"], "hotel": hotel["price_total_rub"],
                                "transfers": tin["price_rub"] + tout["price_rub"]},
            "data_source": "mock",
        }
        with tr.step("validate") as h:
            plan["issues"] = validate_plan(plan, t.total, t.pets, state.budget.total_rub)
            h["detail"] = ", ".join(f"{i['level']}:{i['code']}" for i in plan["issues"]) or "без замечаний"
        state.plan = plan
        state.stage = "plan"
        diff = plan_diff(old_plan, plan)

        cards = [Card(type="plan", data=plan),
                 Card(type="comparison", data={"kind": "hotel", "weights": state.weights.normalized(),
                                               "items": [{**x, "explanation": explain_hotel(x, state, hotels),
                                                          "selected": x["id"] == hotel["id"]} for x in hotels[:3]]}),
                 Card(type="comparison", data={"kind": "transport", "direction": "out",
                                               "items": [{**x, "explanation": explain_transport(x, state),
                                                          "selected": x["id"] == out["id"]} for x in outs[:3]]})]
        if plan["issues"]:
            cards.append(Card(type="issues", data={"items": plan["issues"]}))
        cards.append(self._assumptions_card(state))

        errors = [i for i in plan["issues"] if i["level"] == "error"]
        qr = []
        if not errors:
            qr.append(QuickReply(label=f"Оформить за {_rub(total)}", action={"type": "checkout"}))
        qr += [QuickReply(label=f"Отель: {x['name']}", action={"type": "select_hotel", "id": x["id"]})
               for x in hotels[:3] if x["id"] != hotel["id"]]
        qr += [QuickReply(label="На 2 дня позже", text="а если на 2 дня позже?"),
               QuickReply(label="Дешевле", text="бюджет меньше на 20%")]

        warn = next((i for i in plan["issues"] if i["level"] in ("error", "warning")), None)
        why = plan["hotel"]["explanation"]["why"]
        msg = self._write(tr, {"stage": "plan", "request": describe_request(state), "destination": dest["name"],
                              "dates": f"{start:%d.%m}–{end:%d.%m}", "outbound": {k: out[k] for k in ("carrier", "departure", "duration_h", "stops", "mode")},
                              "hotel": {"name": hotel["name"], "meals": MEAL_NAME[hotel["meals"]], "why": why[:2]},
                              "total_rub": total, "main_issue": warn, "changes": diff, "budget_conflict": conflict,
                              "assumptions": [a.value for a in state.assumptions]}) or (
            (conflict + " " if conflict else "") +
            ("Пересобрал план: " + "; ".join(f"{d['label'].lower()} {d['before']} → {d['after']}" for d in diff) + ". " if diff else "") +
            f"Поездка — {dest['name']}, {start:%d.%m}–{end:%d.%m}: "
            f"{'поезд' if out['mode'] == 'train' else 'рейс'} {out['carrier']} ({out['duration_h']} ч"
            f"{', без пересадок' if not out['stops'] else ', с пересадкой'}), отель «{hotel['name']}» — "
            f"{', '.join(why[:2]) or 'лучший по вашим критериям'}. Итого {_rub(total)}."
            + (f" Внимание: {warn['message']}" + (f" — предлагаю {warn['fix']}." if warn.get('fix') else ".") if warn else ""))
        return AgentResponse(session_id=sid, stage=state.stage, message=msg, cards=cards, quick_replies=qr,
                             diff=diff, trace=tr.steps, state=state)

    def _checkout(self, sid: str, state: TripState, tr: Tracer) -> AgentResponse:
        plan = state.plan
        errors = [i for i in plan["issues"] if i["level"] == "error"]
        if errors:
            return AgentResponse(session_id=sid, stage=state.stage, trace=tr.steps, state=state,
                                 message="Пока не могу оформить: " + "; ".join(e["message"] for e in errors) + ". Давайте поправим план.",
                                 cards=[Card(type="issues", data={"items": plan["issues"]})])
        cart = {"items": [
            {"kind": "transport", "title": f"{plan['outbound']['from']} → {plan['outbound']['to']}", "id": plan["outbound"]["id"], "price_rub": plan["outbound"]["price_rub"]},
            {"kind": "transport", "title": f"{plan['return']['from']} → {plan['return']['to']}", "id": plan["return"]["id"], "price_rub": plan["return"]["price_rub"]},
            {"kind": "hotel", "title": f"{plan['hotel']['name']}, {plan['nights']} ноч." + (f", {plan['hotel']['rooms']} номера" if plan["hotel"].get("rooms") else ""), "id": plan["hotel"]["id"], "price_rub": plan["hotel"]["price_total_rub"]},
            {"kind": "transfer", "title": "Трансфер до отеля", "id": plan["transfer_in"]["id"], "price_rub": plan["transfer_in"]["price_rub"]},
            {"kind": "transfer", "title": "Трансфер обратно", "id": plan["transfer_out"]["id"], "price_rub": plan["transfer_out"]["price_rub"]},
        ], "total_rub": plan["total_rub"], "hotel_id": plan["hotel"]["id"]}
        with tr.step("checkout") as h:
            prev = state.order
            order = get_backend().checkout(cart)
            if prev:
                order["previous_order_id"] = prev["order_id"]
                order["price_difference_rub"] = cart["total_rub"] - prev["paid_rub"]
            state.order = {**order, "cart": cart, "services": [], "reminders": build_reminders(plan)}
            h["detail"] = f"заказ {order['order_id']} (оплата имитирована)"
        if self.memory and state.user_id:
            self.memory.save(state.user_id, profile_from_booking(state, self.today or date.today()))
            tr.add("profile", "запомнил предпочтения для следующих поездок")
        state.stage = "booked"
        diff_note = ""
        if state.order.get("previous_order_id"):
            d = state.order["price_difference_rub"]
            diff_note = f" Бронь {state.order['previous_order_id']} заменена; " + \
                (f"доплата {_rub(d)}." if d > 0 else f"вернём {_rub(-d)}." if d < 0 else "цена не изменилась.")
        return AgentResponse(
            session_id=sid, stage=state.stage, trace=tr.steps, state=state,
            message=f"Готово! Поездка оформлена, номер брони {order['order_id']} (оплата имитирована).{diff_note} "
                    f"Могу запросить ранний заезд, поменять даты или напомнить о вылете.",
            cards=[Card(type="order", data=state.order)], quick_replies=self._booked_replies(state))

    def _booked(self, sid: str, state: TripState, tr: Tracer) -> AgentResponse:
        return AgentResponse(session_id=sid, stage=state.stage, trace=tr.steps, state=state,
                             message=f"Бронь {state.order['order_id']} оформлена. Скажите, что поменять: даты, отель, "
                                     f"бюджет, — я пересоберу план и покажу разницу в цене.",
                             cards=[Card(type="order", data=state.order)], quick_replies=self._booked_replies(state))

    def _early_checkin(self, sid: str, state: TripState, tr: Tracer) -> AgentResponse:
        with tr.step("early_checkin") as h:
            service = request_early_checkin(state.plan, get_backend())
            h["detail"] = service["status_text"]
        services = [s for s in state.order.get("services", []) if s["kind"] != "early_checkin"]
        state.order = {**state.order, "services": services + [service]}
        hotel = state.plan["hotel"]
        msg = {"confirmed": f"Ранний заезд в «{hotel['name']}» подтверждён — можно заселяться сразу по приезде.",
               "on_request": f"Запросил ранний заезд в «{hotel['name']}»: отель подтвердит в день заезда. "
                             f"Если не получится — багаж можно оставить на ресепшен.",
               "requested": f"Отправил отелю «{hotel['name']}» запрос на ранний заезд, ответ покажу в заказе.",
               "declined": f"«{hotel['name']}» не делает ранний заезд (заселение с {hotel['check_in']}). "
                           f"Багаж можно оставить на ресепшен или взять более поздний рейс."}[service["status"]]
        return AgentResponse(session_id=sid, stage=state.stage, trace=tr.steps, state=state, message=msg,
                             cards=[Card(type="order", data=state.order)], quick_replies=self._booked_replies(state))

    def _reminders(self, sid: str, state: TripState, tr: Tracer) -> AgentResponse:
        items = state.order.get("reminders") or build_reminders(state.plan)
        tr.add("reminders", f"{len(items)} напоминаний")
        lines = "; ".join(f"{datetime.fromisoformat(r['at']):%d.%m %H:%M} — {r['text'].lower()}" for r in items[:4])
        return AgentResponse(session_id=sid, stage=state.stage, trace=tr.steps, state=state,
                             message=f"Напомню: {lines}. Полный список — в карточке заказа.",
                             cards=[Card(type="order", data=state.order)], quick_replies=self._booked_replies(state))

    @staticmethod
    def _booked_replies(state: TripState) -> list[QuickReply]:
        qr = []
        if not any(s["kind"] == "early_checkin" for s in state.order.get("services", [])):
            qr.append(QuickReply(label="Запросить ранний заезд", action={"type": "request_early_checkin"}))
        return qr + [QuickReply(label="Напоминания", text="напомни, что когда"),
                     QuickReply(label="На 2 дня позже", text="а если на 2 дня позже?")]

    # ------------------------------------------------------------- утилиты
    @staticmethod
    def _pick(options: list[dict], selected_id: str | None) -> dict:
        return next((o for o in options if o["id"] == selected_id), options[0])

    @staticmethod
    def _pick_transfer(opts: list[dict], state: TripState, when: str) -> dict:
        hour = datetime.fromisoformat(when).hour
        if state.travelers.has_kids or state.travelers.pets or hour >= 22 or hour < 6:
            return next(o for o in opts if o["kind"] == "taxi")
        ok = [o for o in opts if o["minutes"] <= 90]
        return min(ok or opts, key=lambda o: o["price_rub"])

    @staticmethod
    def _assumptions_card(state: TripState) -> Card:
        return Card(type="assumptions", data={
            "items": [a.model_dump() for a in state.assumptions],
            "weights": state.weights.model_dump(),
            "must_have": state.must_have,
            "constraints": state.constraints.model_dump(),
        })
