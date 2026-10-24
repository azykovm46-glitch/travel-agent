"""TripState — единое состояние поездки.

Все шаги агента читают и пишут только этот объект. Пересборка плана =
патч TripState + перезапуск зависимых шагов (см. agent/replan.py).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

Stage = Literal["new", "clarify", "inspire", "plan", "checkout", "booked"]


class Travelers(BaseModel):
    adults: int = 1
    children_ages: list[int] = Field(default_factory=list)
    pets: int = 0

    @property
    def total(self) -> int:
        return self.adults + len(self.children_ages)

    @property
    def has_kids(self) -> bool:
        return bool(self.children_ages)


class Dates(BaseModel):
    start: date | None = None
    end: date | None = None
    month: int | None = None          # если известен только месяц
    nights: int | None = None
    flexibility_days: int = 0

    @property
    def is_known(self) -> bool:
        return self.start is not None or self.month is not None


class Budget(BaseModel):
    total_rub: int | None = None      # на всю поездку и всех гостей
    level: Literal["low", "mid", "high"] | None = None


class Constraints(BaseModel):
    max_travel_hours: float | None = None   # в одну сторону
    direct_only: bool = False
    transport: Literal["any", "plane", "train"] = "any"


class Weights(BaseModel):
    """Веса критериев сравнения. Видны пользователю и правятся в UI."""
    price: float = 0.30
    travel_time: float = 0.15
    location: float = 0.15
    rating: float = 0.15
    cancellation: float = 0.10
    meals: float = 0.15

    def normalized(self) -> dict[str, float]:
        d = self.model_dump()
        s = sum(d.values()) or 1.0
        return {k: v / s for k, v in d.items()}


class Assumption(BaseModel):
    """Допущение агента. Показывается в панели «Как агент думает»."""
    field: str
    value: str
    reason: str
    source: Literal["default", "inferred", "profile"] = "inferred"


class Selection(BaseModel):
    destination_id: str | None = None
    outbound_id: str | None = None
    return_id: str | None = None
    hotel_id: str | None = None
    transfer_in_id: str | None = None
    transfer_out_id: str | None = None
    locked: list[str] = Field(default_factory=list)   # поля, выбранные пользователем вручную


class TripState(BaseModel):
    origin_city: str | None = None
    destination_hint: str | None = None          # «к тёплому морю», «в горы»
    trip_type: list[str] = Field(default_factory=list)   # sea, city, nature, ski
    dates: Dates = Field(default_factory=Dates)
    travelers: Travelers = Field(default_factory=Travelers)
    budget: Budget = Field(default_factory=Budget)
    constraints: Constraints = Field(default_factory=Constraints)
    must_have: list[str] = Field(default_factory=list)   # breakfast, free_cancellation, family_room, pets_allowed, all_inclusive
    weights: Weights = Field(default_factory=Weights)
    assumptions: list[Assumption] = Field(default_factory=list)
    selection: Selection = Field(default_factory=Selection)

    # Результаты инструментов (кэш), чтобы пересборка не искала всё заново
    candidates: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    plan: dict[str, Any] | None = None            # собранный план (см. agent/planner.py)
    stage: Stage = "new"
    confirmed: list[str] = Field(default_factory=list)   # слоты, которые назвал пользователь: origin, dates, travelers
    clarify_rounds: int = 0
    order: dict[str, Any] | None = None

    # ---- вспомогательное -------------------------------------------------
    def add_assumption(self, field: str, value: str, reason: str,
                       source: str = "inferred") -> None:
        self.assumptions = [a for a in self.assumptions if a.field != field]
        self.assumptions.append(Assumption(field=field, value=value, reason=reason, source=source))

    def drop_assumption(self, field: str) -> None:
        self.assumptions = [a for a in self.assumptions if a.field != field]

    def resolved_dates(self, today: date | None = None) -> tuple[date, date] | None:
        """Конкретные даты: если известен только месяц — берём ближайший."""
        today = today or date.today()
        nights = self.dates.nights or 7
        if self.dates.start:
            end = self.dates.end or self.dates.start + timedelta(days=nights)
            return self.dates.start, end
        if self.dates.month:
            year = today.year if self.dates.month > today.month else today.year + 1
            if self.dates.month == today.month:
                year = today.year
            start = date(year, self.dates.month, 10)
            while start.weekday() != 5:        # ближайшая суббота после 10-го
                start += timedelta(days=1)
            if start < today:
                start = today + timedelta(days=14)
            return start, start + timedelta(days=nights)
        return None


def summarize(state: TripState) -> str:
    """Короткое описание состояния для промпта LLM."""
    data = state.model_dump(
        mode="json", exclude={"candidates", "order", "assumptions", "plan"}, exclude_none=True)
    return json.dumps(data, ensure_ascii=False)
