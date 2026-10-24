# Контракты между ролями

Отправь этот файл участникам 2 и 3 в первый час. Любое изменение — через этот файл.

## 1. Инструменты (роль A ↔ роль B)

Агент вызывает функции модуля, который задаёт `TOOLS_BACKEND` (по умолчанию
`tools.mock_backend`). Роль B делает `tools/real_backend.py` с теми же сигнатурами и
переключает переменную окружения. Проверка совместимости: `tools/__init__.py` падает при
старте, если функции не хватает. Все ответы — списки словарей с полем `data_source`.

| Функция | Аргументы | Возвращает (ключевые поля) |
| --- | --- | --- |
| `search_destinations` | origin, month, trip_type, max_travel_hours, budget_per_night, travelers, nights | `id, name, country, tags, visa, air_temp, water_temp, flight_hours, train_hours, est_total_rub, est_night_rub` |
| `get_weather` | dest_id, month | `air_temp, water_temp` |
| `search_transport` | origin, dest_id, day, travelers, direction (`out`/`back`) | `id, mode (plane/train), carrier, from, to, departure, arrival (ISO), duration_h, stops, baggage, price_rub` (за всех) |
| `search_hotels` | dest_id, check_in, check_out, adults, children, pets | `id, name, stars, price_per_night_rub, price_total_rub, nights, meals (RO/BB/HB/AI), free_cancellation, cancellation_deadline_days, max_guests, family_rooms, pets_allowed, kids_club, beach_m, center_km, airport_km, check_in, check_out (HH:MM), early_checkin, rating, reviews, review_pros, review_cons` |
| `search_transfers` | dest_id, from_point, to_point (`airport`/`station`/`hotel:<id>`), when, travelers, pets | `id, kind (taxi/shuttle/public), minutes, price_rub, pets_ok` |
| `checkout` | cart | `order_id, status, paid_rub, payment` |
| `get_destination`, `find_destination_by_text`, `normalize_origin` | справочник | словарь направления / название города |

Требования к B: детерминизм (одинаковый запрос → одинаковый ответ), ответ < 1 с,
никогда не бросать исключение на странных входах — вернуть пустой список.

## 2. API (роль A ↔ роль C)

`POST /api/chat`
```json
{"session_id": "abc123 или null", "message": "текст пользователя", "action": null}
```
Вместо `message` фронт может прислать структурное действие `action`:

| action.type | поля | когда |
| --- | --- | --- |
| `select_destination` | `id` | клик «Выбрать» на карточке направления |
| `select_hotel` | `id` | клик по отелю в сравнении |
| `select_transport` | `id`, `direction` (`out`/`back`) | клик по рейсу |
| `checkout` | — | кнопка «Оформить» |
| `set_weights` | `weights: {price, travel_time, location, rating, cancellation, meals}` | ползунки в панели логики |
| `remove_assumption` | `field` | крестик на допущении |
| `update` | `patch` — любые поля патча (см. `agent/prompts.py`) | ручная правка условий |

Ответ (`AgentResponse`, см. `agent/schemas.py`):
```json
{
  "session_id": "abc123", "stage": "clarify|inspire|plan|booked",
  "message": "текст агента",
  "questions": [{"slot": "origin", "text": "Откуда?", "options": [{"label": "Москва", "text": "из Москвы"}]}],
  "cards": [{"type": "destination|plan|comparison|issues|assumptions|order", "data": {}}],
  "quick_replies": [{"label": "Оформить за 158 218 ₽", "action": {"type": "checkout"}}],
  "diff": [{"field": "dates", "label": "Даты", "before": "14.11–21.11", "after": "16.11–23.11"}],
  "trace": [{"step": "search_hotels", "detail": "14 отелей", "ms": 3}],
  "state": {"...": "TripState целиком"}
}
```
Кнопка (`QuickReply`) содержит либо `text` — отправить как сообщение, либо `action` — отправить как действие.

Что рисовать по типам карточек:
- `destination` — название, страна, воздух/вода, часы в пути, ориентир цены, `explanation.why/tradeoffs/risks`
- `plan` — лента: вылет → прилёт → трансфер → заселение → выезд → трансфер → вылет; `issues` светофором
- `comparison` (`kind: hotel|transport`) — 3 варианта, `selected`, `breakdown` по критериям, `explanation`
- `assumptions` — список допущений с крестиком + ползунки `weights`
- `issues` — `level` error/warning/info, `message`, `fix` (кнопка-предложение)
- `order` — номер брони, корзина, сумма
- `trace` — панель «Как агент думает» (шаги и время)
- `diff` — подсветка «было → стало» после пересборки

## 3. Метрики (роль A ↔ роль D)
`python evals/run.py [--offline]` — запросы в `evals/queries.jsonl`, роль D пополняет до 50.
Формат строки: `{"id": "...", "text": "..."}`. Итог: completion, clarify_avg, first_rec_s_median, stitch_err, explained.
