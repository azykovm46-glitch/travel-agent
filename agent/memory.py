"""Память в SQLite: профиль пользователя между сессиями и сами сессии.

SessionStore — TripState каждой сессии: перезапуск сервера (uvicorn --reload сохраняет
файл → перезапуск) посреди демо не теряет разговор.

user_id -> предпочтения из прошлых броней: город вылета, состав семьи, питание,
ценовой уровень, обязательные условия и отказы (убранные допущения).
Сохраняем после брони. В новой сессии подставляем как допущения source="profile":
пользователь видит «из профиля» и может убрать любое (remove_assumption с field="profile_*").
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from agent.state import TripState

MEAL_LABEL = {"BB": "завтраки", "HB": "завтрак и ужин", "AI": "всё включено"}


@contextmanager
def _db(path: Path):
    """Транзакция и обязательное закрытие: `with sqlite3.connect()` само соединение не закрывает."""
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class SessionStore:
    """Сессии: кэш в памяти + запись в SQLite после каждого хода. path=None — только в памяти."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._cache: dict[str, TripState] = {}
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _db(self.path) as c:
                c.execute("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, data TEXT NOT NULL, "
                          "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")

    def get(self, session_id: str) -> TripState | None:
        if session_id in self._cache or not self.path:
            return self._cache.get(session_id)
        with _db(self.path) as c:
            row = c.execute("SELECT data FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row:
            self._cache[session_id] = TripState.model_validate_json(row[0])
        return self._cache.get(session_id)

    def save(self, session_id: str, state: TripState) -> None:
        self._cache[session_id] = state
        if self.path:
            with _db(self.path) as c:
                c.execute("INSERT INTO sessions (session_id, data) VALUES (?, ?) ON CONFLICT(session_id) "
                          "DO UPDATE SET data = excluded.data, updated_at = CURRENT_TIMESTAMP",
                          (session_id, state.model_dump_json()))


class ProfileStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _db(self.path) as c:   # соединение на операцию — безопасно для потоков API
            c.execute("CREATE TABLE IF NOT EXISTS profiles (user_id TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def get(self, user_id: str) -> dict | None:
        with _db(self.path) as c:
            row = c.execute("SELECT data FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, user_id: str, profile: dict) -> None:
        with _db(self.path) as c:
            c.execute("INSERT INTO profiles (user_id, data) VALUES (?, ?) "
                      "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
                      (user_id, json.dumps(profile, ensure_ascii=False)))


def profile_from_booking(state: TripState, today: date) -> dict:
    """Что запомнить после брони."""
    t, hotel = state.travelers, (state.plan or {}).get("hotel", {})
    return {
        "origin_city": state.origin_city,
        "adults": t.adults, "children_ages": t.children_ages, "pets": t.pets,
        "budget_level": state.budget.level,
        "meals": hotel.get("meals"),
        "must_have": [m for m in state.must_have if m != "pets_allowed"],   # питомец — из состава
        "declined": sorted({c for c in state.confirmed if c.startswith("no:")}),
        "saved_at": today.isoformat(),
    }


def apply_profile(state: TripState, profile: dict, today: date) -> None:
    """Подставить профиль в новую сессию: значения + видимые допущения «из профиля»."""
    why = f"из профиля: бронь {date.fromisoformat(profile['saved_at']):%d.%m.%Y}"
    if profile.get("origin_city"):
        state.origin_city = profile["origin_city"]
        state.confirmed.append("origin")
        state.add_assumption("profile_origin", f"из города {profile['origin_city']}", why, "profile")
    years = today.year - date.fromisoformat(profile["saved_at"]).year     # дети подросли
    ages = [a + years for a in profile.get("children_ages", []) if a + years < 18]
    state.travelers.adults = profile.get("adults") or 1
    state.travelers.children_ages = ages
    state.travelers.pets = profile.get("pets", 0)
    state.confirmed += ["travelers", "adults"]
    who = f"{state.travelers.adults} взр." + (f" + дети {', '.join(map(str, ages))} лет" if ages else "") \
        + (" + питомец" if state.travelers.pets else "")
    state.add_assumption("profile_travelers", who, why, "profile")
    if profile.get("budget_level"):
        state.budget.level = profile["budget_level"]
        label = {"low": "недорого", "mid": "средний уровень", "high": "премиум"}[profile["budget_level"]]
        state.add_assumption("profile_budget", label, why, "profile")
    if profile.get("must_have"):
        state.must_have = list(profile["must_have"])
        state.add_assumption("profile_must_have", ", ".join(profile["must_have"]), why, "profile")
    if profile.get("meals") in MEAL_LABEL:
        state.weights.meals += 0.1
        state.add_assumption("profile_meals", f"питание в приоритете (в прошлый раз — {MEAL_LABEL[profile['meals']]})",
                             why, "profile")
    state.confirmed += [d for d in profile.get("declined", []) if d not in state.confirmed]


def forget_profile_field(state: TripState, field: str) -> set[str]:
    """Пользователь убрал допущение «из профиля» — откатываем значение. Возвращает изменённые группы."""
    state.drop_assumption(field)
    if field == "profile_origin":
        state.origin_city = None
        state.confirmed = [c for c in state.confirmed if c != "origin"]
        return {"origin_city"}
    if field == "profile_travelers":
        state.travelers = type(state.travelers)()
        state.confirmed = [c for c in state.confirmed if c not in ("travelers", "adults")]
        return {"travelers"}
    if field == "profile_budget":
        state.budget.level = None
        return {"budget"}
    if field == "profile_must_have":
        state.must_have = [m for m in state.must_have if m == "pets_allowed"]
        return {"must_have"}
    if field == "profile_meals":
        state.weights.meals = max(0.0, state.weights.meals - 0.1)
        return {"weights"}
    return set()
