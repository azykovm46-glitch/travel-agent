"""Проверка состыковок собранного плана.

Возвращает список проблем: level = error | warning | info.
error — план нельзя оформлять; warning — можно, но предупреждаем и
предлагаем действие (fix); info — полезная заметка.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def validate_plan(plan: dict, travelers_total: int, pets: int, budget_rub: int | None) -> list[dict]:
    issues: list[dict] = []
    out, back, hotel = plan.get("outbound"), plan.get("return"), plan.get("hotel")
    tin, tout = plan.get("transfer_in"), plan.get("transfer_out")
    check_in_d, check_out_d = plan["check_in"], plan["check_out"]

    def add(level, code, message, fix=None):
        issues.append({"level": level, "code": code, "message": message, "fix": fix})

    if not (out and back and hotel):
        add("error", "incomplete", "В плане не хватает перелёта/поезда или отеля")
        return issues

    arrival = _t(out["arrival"])
    at_hotel = arrival + timedelta(minutes=tin["minutes"] if tin else 60)
    hotel_checkin = datetime.combine(datetime.fromisoformat(check_in_d).date(),
                                     datetime.strptime(hotel["check_in"], "%H:%M").time())
    hotel_checkout = datetime.combine(datetime.fromisoformat(check_out_d).date(),
                                      datetime.strptime(hotel["check_out"], "%H:%M").time())

    # Ночной заезд до 06:00 следующего дня — нормально (номер держат), это late_arrival ниже.
    night_end = datetime.combine(hotel_checkin.date() + timedelta(days=1), datetime.min.time()) + timedelta(hours=6)
    if at_hotel > night_end:
        add("error", "arrive_after_checkin_day",
            f"Прибытие {at_hotel:%d.%m %H:%M} — позже дня заезда, первая ночь оплачена впустую",
            "сдвинуть заезд на день позже или взять более ранний рейс")
    elif at_hotel < hotel_checkin - timedelta(hours=2):
        wait = (hotel_checkin - at_hotel)
        h = round(wait.total_seconds() / 3600, 1)
        if hotel.get("early_checkin"):
            add("warning", "early_arrival", f"Будете в отеле в {at_hotel:%H:%M}, заселение с {hotel['check_in']} — ждать ~{h:g} ч",
                "запросить ранний заезд (отель его допускает)")
        else:
            add("warning", "early_arrival", f"Будете в отеле в {at_hotel:%H:%M}, заселение с {hotel['check_in']} — ждать ~{h:g} ч, ранний заезд отель не гарантирует",
                "оставить багаж на ресепшен или выбрать более поздний рейс")
    if at_hotel.hour >= 23 or at_hotel.hour < 5:
        add("warning", "late_arrival", f"Приезд в отель в {at_hotel:%H:%M} — предупредите отель о позднем заселении",
            "отправить отелю уведомление")

    departure = _t(back["departure"])
    leave_hotel = departure - timedelta(minutes=(tout["minutes"] if tout else 60) + (120 if back["mode"] == "plane" else 40))
    if departure.date() != hotel_checkout.date():
        add("error", "dates_mismatch",
            f"Обратный рейс {departure:%d.%m}, а выезд из отеля {hotel_checkout:%d.%m}",
            "выровнять даты отеля и обратного билета")
    elif leave_hotel > hotel_checkout + timedelta(hours=3):
        h = round((leave_hotel - hotel_checkout).total_seconds() / 3600, 1)
        add("info", "late_departure", f"После выезда в {hotel['check_out']} до дороги ещё ~{h:g} ч",
            "запросить поздний выезд или камеру хранения")
    elif leave_hotel < hotel_checkout - timedelta(hours=8):
        add("info", "early_departure", f"Выезжать из отеля нужно в {leave_hotel:%H:%M} — рано утром")

    if hotel["max_guests"] < travelers_total:
        add("error", "capacity", f"Номер до {hotel['max_guests']} гостей, а едет {travelers_total}")
    if pets and not hotel.get("pets_allowed"):
        add("error", "pets", "Отель не принимает животных")
    if pets and tin and not tin.get("pets_ok", True):
        add("error", "pets_transfer", "Выбранный трансфер не перевозит животных", "взять такси")

    total = plan.get("total_rub", 0)
    if budget_rub and total > budget_rub:
        over = total - budget_rub
        add("warning", "over_budget",
            f"Итог {total:,} ₽ — выше бюджета на {over:,} ₽".replace(",", " "),
            "выбрать отель дешевле или сдвинуть даты")
    if not hotel.get("free_cancellation"):
        add("info", "no_free_cancel", "Отель без бесплатной отмены — деньги не вернут при отмене")
    return issues
