"""После брони: напоминания и запрос раннего заезда.

Всё считается из собранного плана — без выдуманных фактов. Смена дат после брони
идёт обычной пересборкой (planner.handle), разница в цене — при повторном оформлении.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

EARLY_STATUS = {"confirmed": "подтверждён", "on_request": "под запрос — отель ответит в день заезда",
                "requested": "запрос отправлен", "declined": "отель не делает ранний заезд"}
TRANSFER_NAME = {"taxi": "такси", "shuttle": "шаттл", "public": "общественный транспорт"}


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def build_reminders(plan: dict) -> list[dict]:
    """Список напоминаний {at, text}, по времени."""
    out, back, hotel = plan["outbound"], plan["return"], plan["hotel"]
    tin, tout = plan.get("transfer_in") or {}, plan.get("transfer_out") or {}
    rows = []
    dep = _dt(out["departure"])
    if out["mode"] == "plane":
        rows.append((dep - timedelta(hours=24), f"Открывается онлайн-регистрация на рейс {out['carrier']} {out['from']} → {out['to']}"))
        rows.append((dep - timedelta(hours=3), f"Выезжайте в аэропорт: вылет в {dep:%H:%M}"))
    else:
        rows.append((dep - timedelta(hours=1), f"Будьте на вокзале: поезд {out['from']} → {out['to']} в {dep:%H:%M}"))
    check_in = datetime.combine(date.fromisoformat(plan["check_in"]), datetime.strptime(hotel["check_in"], "%H:%M").time())
    rows.append((check_in, f"Заселение в «{hotel['name']}» с {hotel['check_in']}"
                           + (f", трансфер из {'аэропорта' if out['mode'] == 'plane' else 'вокзала'} — {TRANSFER_NAME.get(tin.get('kind'), 'такси')}" if tin else "")))
    if hotel.get("free_cancellation"):
        deadline = check_in - timedelta(days=hotel.get("cancellation_deadline_days", 1))
        rows.append((deadline, "Последний день бесплатной отмены отеля"))
    ret = _dt(back["departure"])
    if back["mode"] == "plane":
        rows.append((ret - timedelta(hours=24), f"Онлайн-регистрация на обратный рейс {back['carrier']}"))
    leave = ret - timedelta(minutes=tout.get("minutes", 60) + (120 if back["mode"] == "plane" else 40))
    rows.append((leave, f"Выезд из отеля к {'рейсу' if back['mode'] == 'plane' else 'поезду'} в {ret:%H:%M}"
                        + (f" ({TRANSFER_NAME.get(tout['kind'], tout['kind'])}, ~{tout['minutes']} мин в пути)" if tout else "")))
    return [{"at": at.isoformat(timespec="minutes"), "text": text} for at, text in sorted(rows)]


def request_early_checkin(plan: dict, backend) -> dict:
    """Запросить ранний заезд. Функция бэкенда необязательна: без неё — «запрос отправлен»."""
    hotel = plan["hotel"]
    if hotel.get("early_checkin") is False:          # None — бэкенд не знает: не отказываем за отель
        status = "declined"
    elif fn := getattr(backend, "request_early_checkin", None):
        status = fn(hotel["id"], date.fromisoformat(plan["check_in"]), plan["outbound"]["arrival"]).get("status", "requested")
    else:
        status = "requested"
    status = status if status in EARLY_STATUS else "requested"
    return {"kind": "early_checkin", "title": "Ранний заезд", "status": status, "status_text": EARLY_STATUS[status]}
