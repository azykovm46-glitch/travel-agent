"""Оркестратор агента: одна точка входа handle().

Поток одного хода:
  сообщение/действие -> патч (правила + LLM) -> TripState -> скрытые требования
  -> пересборка (сброс устаревшего) -> следующий шаг по политике
  -> инструменты + скоринг + проверка стыковок -> карточки + объяснения + сообщение.
"""
from __future__ import annotations

import copy
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime

from agent import policy
from agent.explain import _rub, explain_destination, explain_hotel, explain_transport, MEAL_NAME
from agent.llm import LLM
from agent.replan import invalidate, plan_diff
from agent.schemas import AgentResponse, Card, QuickReply, TraceStep
from agent.state import TripState, summarize
from agent.understand import apply_patch, infer_hidden, merge_patches, rule_patch
from tools import get_backend
from tools.scoring import hotel_filters, score_destinations, score_hotels, score_transport
from tools.validator import validate_plan


class Tracer:
    def __init__(self) -> None:
        self.steps: list[TraceStep] = []

    @contextmanager
    def step(self, name: str, detail: str = ""):
        t0 = time.perf_counter()
        holder = {"detail": detail}
        yield holder
        self.steps.append(TraceStep(step=name, detail=holder["detail"], ms=int((time.perf_counter() - t0) * 1000)))

    def add(self, name: str, detail: str) -> None:
        self.steps.append(TraceStep(step=name, detail=detail))


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
    who = f"{t.adults} взр."
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
    def __init__(self, llm: LLM | None = None, today: date | None = None) -> None:
        self.llm = llm or LLM()
        self.today = today
        self.sessions: dict[str, TripState] = {}

    # ------------------------------------------------------------------ API
    def get_state(self, session_id: str) -> TripState | None:
        return self.sessions.get(session_id)

    def handle(self, session_id: str | None = None, message: str | None = None,
               action: dict | None = None) -> AgentResponse:
        sid = session_id or uuid.uuid4().hex[:12]
        state = self.sessions.setdefault(sid, TripState())
        tr = Tracer()
        old_plan = copy.deepcopy(state.plan)
        today = self.today or date.today()

        patch: dict = {}
        if message:
            with tr.step("understand") as h:
                rules = rule_patch(message, state)
                llm_patch = self.llm.extract_patch(message, summarize(state), today.isoformat())
                patch = merge_patches(rules, llm_patch)
                h["detail"] = ("LLM + правила" if llm_patch else "правила (LLM выключена)") + \
                    f": {', '.join(k for k in patch if k != 'intent') or 'без новых данных'}"
        extra_changed: set[str] = set()
        if action:
            patch, extra_changed = self._action_to_patch(action, state, patch)

        intent = patch.get("intent", "plan")
        changed = apply_patch(state, patch) | extra_changed
        infer_hidden(state)

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
        policy.apply_defaults(state, today)
        infer_hidden(state)
        if step == "inspire":
            return self._inspire(sid, state, tr)
        return self._plan(sid, state, tr, old_plan)

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
        if not sel_id and idx:
            top = state.candidates.get(pool, [])
            sel_id = top[idx - 1]["id"] if 0 < idx <= len(top) else None
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
        msg = self.llm.write({"stage": "clarify", "understood": understood,
                              "questions": [q.text for q in qs]}) or \
            f"Понял: {understood}. Чтобы подобрать варианты, уточню: " + " ".join(q.text for q in qs)
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
        best = top[0]
        msg = self.llm.write({"stage": "inspire", "request": describe_request(state),
                              "options": [{k: d[k] for k in ("name", "country", "air_temp", "water_temp", "flight_hours",
                                                             "est_total_rub", "reasons", "tradeoffs")} for d in top],
                              "assumptions": [a.value for a in state.assumptions]}) or (
            f"Под запрос ({describe_request(state)}) подходят: {', '.join(d['name'] for d in top)}. "
            f"Лучше всего — {best['name']}: {', '.join(best['reasons'][:2]) or 'хороший баланс цены и дороги'}, "
            f"ориентировочно {_rub(best['est_total_rub'])} на всех. Выберите направление — соберу поездку целиком.")
        return AgentResponse(session_id=sid, stage=state.stage, message=msg, cards=cards,
                             quick_replies=qr, trace=tr.steps, state=state)

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
            return AgentResponse(session_id=sid, stage=state.stage, trace=tr.steps, state=state,
                                 message="Не нашёл отелей, которые подходят под все условия ("
                                         + "; ".join(sorted(reasons)[:3]) + "). Могу ослабить одно из условий или предложить другое направление.",
                                 quick_replies=[QuickReply(label="Другие направления", action={"type": "update", "patch": {"intent": "plan"}})])

        # Стыковка закладывается в выбор: рейсы, прилетающие в день заезда, — в приоритете.
        # Если все прилетают позже (дальний или ночной перелёт), сдвигаем заезд на день прилёта.
        sel = state.selection
        same_day = [o for o in outs if o["arrival"][:10] <= start.isoformat()]
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
        out_pool = [o for o in outs if o["id"] == sel.outbound_id] or \
            ([o for o in outs if o["arrival"][:10] <= start.isoformat()] or outs)[:4]
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
            fitting = [c for c in combos if not budget or c[1] <= budget]
            if fitting:
                best = max(fitting, key=lambda c: c[0])
                h["detail"] = f"проверено {len(combos)} комбинаций, в бюджет укладываются {len(fitting)}"
            else:
                best = min(combos, key=lambda c: c[1])
                h["detail"] = f"проверено {len(combos)} комбинаций, в бюджет не влезает ни одна — беру самую дешёвую"
            _, total, out, back, hotel, tin, tout = best
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
        msg = self.llm.write({"stage": "plan", "request": describe_request(state), "destination": dest["name"],
                              "dates": f"{start:%d.%m}–{end:%d.%m}", "outbound": {k: out[k] for k in ("carrier", "departure", "duration_h", "stops", "mode")},
                              "hotel": {"name": hotel["name"], "meals": MEAL_NAME[hotel["meals"]], "why": why[:2]},
                              "total_rub": total, "main_issue": warn, "changes": diff,
                              "assumptions": [a.value for a in state.assumptions]}) or (
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
            {"kind": "hotel", "title": f"{plan['hotel']['name']}, {plan['nights']} ноч.", "id": plan["hotel"]["id"], "price_rub": plan["hotel"]["price_total_rub"]},
            {"kind": "transfer", "title": "Трансфер до отеля", "id": plan["transfer_in"]["id"], "price_rub": plan["transfer_in"]["price_rub"]},
            {"kind": "transfer", "title": "Трансфер обратно", "id": plan["transfer_out"]["id"], "price_rub": plan["transfer_out"]["price_rub"]},
        ], "total_rub": plan["total_rub"], "hotel_id": plan["hotel"]["id"]}
        with tr.step("checkout") as h:
            prev = state.order
            order = get_backend().checkout(cart)
            if prev:
                order["previous_order_id"] = prev["order_id"]
                order["price_difference_rub"] = cart["total_rub"] - prev["paid_rub"]
            state.order = {**order, "cart": cart}
            h["detail"] = f"заказ {order['order_id']} (оплата имитирована)"
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
            cards=[Card(type="order", data=state.order)],
            quick_replies=[QuickReply(label="На 2 дня позже", text="а если на 2 дня позже?")])

    def _booked(self, sid: str, state: TripState, tr: Tracer) -> AgentResponse:
        return AgentResponse(session_id=sid, stage=state.stage, trace=tr.steps, state=state,
                             message=f"Бронь {state.order['order_id']} оформлена. Скажите, что поменять: даты, отель, "
                                     f"бюджет, — я пересоберу план и покажу разницу в цене.",
                             cards=[Card(type="order", data=state.order)])

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
