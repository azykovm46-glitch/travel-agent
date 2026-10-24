"""Пересборка плана без поиска с нуля.

Изменение условий -> какие результаты инструментов устарели -> удаляем только их.
Выборы пользователя (selection.locked) сохраняются, если остаются валидными.
"""
from __future__ import annotations

from agent.state import TripState

# группа изменённых полей -> какие кэши инструментов сбросить
DEPS: dict[str, set[str]] = {
    "origin_city": {"destinations", "outbound", "return", "transfers"},
    "dates": {"destinations", "outbound", "return", "hotels", "transfers"},
    "travelers": {"outbound", "return", "hotels", "transfers"},
    "destination": {"outbound", "return", "hotels", "transfers"},
    "trip_type": {"destinations"},
    # только пересчёт ранжирования — кэш не трогаем
    "budget": set(), "constraints": set(), "must_have": set(), "weights": set(),
}
# какие автоматические выборы сбросить при изменении группы
RESELECT: dict[str, list[str]] = {
    "origin_city": ["outbound_id", "return_id", "transfer_in_id", "transfer_out_id"],
    "dates": ["outbound_id", "return_id", "transfer_in_id", "transfer_out_id"],
    "travelers": ["outbound_id", "return_id", "hotel_id", "transfer_in_id", "transfer_out_id"],
    "destination": ["outbound_id", "return_id", "hotel_id", "transfer_in_id", "transfer_out_id"],
    "budget": ["outbound_id", "return_id", "hotel_id"],
    "constraints": ["outbound_id", "return_id"],
    "must_have": ["hotel_id"],
    "weights": ["outbound_id", "return_id", "hotel_id"],
    "trip_type": [],
}


def invalidate(state: TripState, changed: set[str]) -> dict:
    """Возвращает отчёт: что сброшено и что сохранено (для trace и UI)."""
    dropped_cache: set[str] = set()
    for group in changed:
        dropped_cache |= DEPS.get(group, set())
    for key in dropped_cache:
        state.candidates.pop(key, None)
    reset = set()
    for group in changed:
        for field in RESELECT.get(group, []):
            # смену направления пользователь сделал сам — его отель там уже не валиден
            if field in state.selection.locked and group != "destination":
                continue
            if getattr(state.selection, field):
                setattr(state.selection, field, None)
                reset.add(field)
            if field in state.selection.locked:
                state.selection.locked.remove(field)
    kept = [k for k in ("destinations", "outbound", "return", "hotels", "transfers") if k in state.candidates]
    return {"changed": sorted(changed), "dropped": sorted(dropped_cache), "kept": kept, "reset": sorted(reset)}


def plan_diff(old: dict | None, new: dict | None) -> list[dict]:
    if not old or not new:
        return []
    rows = []

    def row(field, label, fmt):
        a, b = fmt(old), fmt(new)
        if a != b:
            rows.append({"field": field, "label": label, "before": a, "after": b})

    dm = lambda s: f"{s[8:10]}.{s[5:7]}"   # noqa: E731  YYYY-MM-DD -> DD.MM
    row("destination", "Направление", lambda p: p["destination"]["name"])
    row("dates", "Даты", lambda p: f"{dm(p['check_in'])}–{dm(p['check_out'])}")
    row("outbound", "Туда", lambda p: f"{p['outbound']['carrier']} {dm(p['outbound']['departure'])} {p['outbound']['departure'][11:16]}")
    row("return", "Обратно", lambda p: f"{p['return']['carrier']} {dm(p['return']['departure'])} {p['return']['departure'][11:16]}")
    row("hotel", "Отель", lambda p: p["hotel"]["name"])
    row("total", "Итого", lambda p: f"{p['total_rub']:,} ₽".replace(",", " "))
    return rows
