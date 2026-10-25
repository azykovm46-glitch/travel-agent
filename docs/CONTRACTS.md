# Контракты между ролями

Отправь этот файл участникам 2 и 3 в первый час. Любое изменение — через этот файл.

## 1. Инструменты (роль A ↔ роль B)

Агент вызывает функции модуля, который задаёт `TOOLS_BACKEND` (по умолчанию
`tools.mock_backend`). Роль B делает `tools/real_backend.py` с теми же сигнатурами и
переключает переменную окружения. Проверка совместимости: `tools/__init__.py` падает при
старте, если функции не хватает. Все ответы — списки словарей с полем `data_source`.

| Функция | Аргументы | Возвращает (ключевые поля) |
| --- | --- | --- |
| `search_destinations` | origin, month, trip_type, max_travel_hours, budget_per_night, travelers, nights | `id, name, country, tags, visa, air_temp, water_temp, flight_hours, train_hours, est_total_rub, est_night_rub, ski_season` (tags может содержать `ski`) |
| `get_weather` | dest_id, month | `air_temp, water_temp` |
| `search_transport` | origin, dest_id, day, travelers, direction (`out`/`back`) | `id, mode (plane/train), carrier, from, to, departure, arrival (ISO), duration_h, stops, baggage, price_rub` (за всех) |
| `search_hotels` | dest_id, check_in, check_out, adults, children, pets | `id, name, stars, price_per_night_rub, price_total_rub, nights, meals (RO/BB/HB/AI), free_cancellation, cancellation_deadline_days, max_guests, family_rooms, pets_allowed, kids_club, beach_m, center_km, airport_km, check_in, check_out (HH:MM), early_checkin, rating, reviews, review_pros, review_cons` |
| `search_transfers` | dest_id, from_point, to_point (`airport`/`station`/`hotel:<id>`), when, travelers, pets | `id, kind (taxi/shuttle/public), minutes, price_rub, pets_ok` |
| `checkout` | cart | `order_id, status, paid_rub, payment` |
| `get_destination`, `find_destination_by_text`, `normalize_origin` | справочник | словарь направления / название города |

Требования к B: детерминизм (одинаковый запрос → одинаковый ответ), ответ < 1 с,
никогда не бросать исключение на странных входах — вернуть пустой список.
`find_destination_by_text` и `normalize_origin` должны понимать косвенные падежи («в Анталью», «из Самары»)
и возвращать `None` для неизвестного — агент тогда честно скажет пользователю, что места нет в каталоге.

Необязательная функция: `request_early_checkin(hotel_id, check_in: date, arrival: str) -> {"status": "confirmed" | "on_request"}`.
Если её нет, агент пишет «запрос отправлен» (статус `requested`); если у отеля `early_checkin=false` — сам отвечает «нет».
`early_checkin=null` значит «неизвестно» — агент не отказывает за отель, а отправляет запрос.

Необязательные поля и атрибуты (агент работает и без них):
- модуль: `SUPPORTED_ORIGINS` — список городов вылета; уточняющий вопрос «Откуда?» предлагает только их;
- `search_destinations`: `travel_note` — оговорка о маршруте, агент показывает её в минусах направления;
- `search_transport`: `note` — то же для рейса, в плане становится предупреждением `route_note`;
  `departure_utc_offset` / `arrival_utc_offset` — пояса; `departure` и `arrival` всегда в местном времени города;
- `search_hotels`: `stars` и `reviews` могут быть `null` (нет в источнике), `review_pros` / `review_cons` — пустыми.

Реальный бэкенд участника 2 подключается адаптером `tools/real_backend.py`
(`TOOLS_BACKEND=tools.real_backend`, путь к репозиторию — `HACKATHON_BACKEND_DIR`, по умолчанию `../hackathon-backend`).
Его код не меняем: справочник направлений и климат — в адаптере, вылеты пока только из Москвы.

## 2. API (роль A ↔ роль C)

`POST /api/chat`
```json
{"session_id": "abc123 или null", "message": "текст пользователя", "action": null, "user_id": "необязательно"}
```
`user_id` включает память между сессиями: после брони агент запоминает город, состав семьи, питание,
ценовой уровень и отказы; в новой сессии этого пользователя подставляет их как допущения с `source: "profile"`
(поле допущения начинается с `profile_`). Крестик на таком допущении откатывает значение.
Вместо `message` фронт может прислать структурное действие `action`:

| action.type | поля | когда |
| --- | --- | --- |
| `select_destination` | `id` | клик «Выбрать» на карточке направления |
| `select_hotel` | `id` | клик по отелю в сравнении |
| `select_transport` | `id`, `direction` (`out`/`back`) | клик по рейсу |
| `checkout` | — | кнопка «Оформить» |
| `set_weights` | `weights: {price, travel_time, location, rating, cancellation, meals}` | ползунки в панели логики |
| `remove_assumption` | `field` | крестик на допущении (в т.ч. `profile_*` — забыть значение из профиля) |
| `change_destination` | — | «Другие направления»: сбросить выбранное место, снова подбор |
| `request_early_checkin` | — | после брони: запросить ранний заезд |
| `reminders` | — | после брони: показать напоминания текстом |
| `update` | `patch` — любые поля патча (см. `agent/prompts.py`) | ручная правка условий |

Например, `{"type": "update", "patch": {"prefer": "location", "near": "beach"}}` — «ближе к морю»: пересортировка
без нового поиска, при готовом плане — только отели ближе к пляжу, чем текущий.
Агент сам присылает такие кнопки: на лыжи не в сезон — первая кнопка «Перенести на ноябрь»
с `{"type": "update", "patch": {"month": 11}}`; фронтенду достаточно отправить `action` как есть.

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
  "state": {"...": "TripState без candidates (внутренний кэш поиска)"}
}
```
Готовые ответы на каждом этапе (clarify, inspire, plan, replan, booked, неизвестное место) —
`docs/api_examples.json`, перегенерировать: `python scripts/make_api_examples.py`.
### Стриминг: `WS /api/chat/ws`
Те же JSON, что в `POST /api/chat`, сколько угодно ходов в одном соединении. На каждый ход сервер шлёт:
```json
{"type": "step_start", "step": "search_hotels", "label": "ищу отели…"}
{"type": "trace", "step": "search_hotels", "detail": "14 отелей", "ms": 3}
{"type": "response", "data": {"...": "AgentResponse, как из POST"}}
{"type": "error", "detail": "нужно message или action"}
```
`label` — что показать в панели «Как агент думает», пока шаг идёт; `response` всегда последний (или `error`).
REST остаётся как был — WebSocket нужен только для живой панели шагов.

`GET /api/health` показывает, включена ли LLM, доступна ли сейчас (`llm_available`), в каком режиме
работает (`llm_mode`) и последнюю ошибку (`llm_last_error`).
Кнопка (`QuickReply`) содержит либо `text` — отправить как сообщение, либо `action` — отправить как действие.

Что рисовать по типам карточек:
- `destination` — название, страна, воздух/вода, часы в пути, ориентир цены, `explanation.why/tradeoffs/risks`
- `plan` — лента: вылет → прилёт → трансфер → заселение → выезд → трансфер → вылет; `issues` светофором.
  Если в отеле есть `rooms` (2–3) — компания не влезла в один номер, цена уже за все номера
- `comparison` (`kind: hotel|transport`) — 3 варианта, `selected`, `breakdown` по критериям, `explanation`
- `assumptions` — список допущений с крестиком + ползунки `weights`
- `issues` — `level` error/warning/info, `message`, `fix` (кнопка-предложение).
  Коды: `arrive_after_checkin_day`, `dates_mismatch`, `capacity`, `pets`, `pets_transfer` (error);
  `early_arrival`, `late_arrival`, `leave_day_before`, `over_budget` (warning);
  `late_departure`, `early_departure`, `several_rooms`, `no_free_cancel` (info)
- `order` — номер брони, корзина, сумма; `services` — доп. услуги со статусом
  (`{kind: "early_checkin", title, status: confirmed|on_request|requested|declined, status_text}`);
  `reminders` — `[{at: ISO, text}]` по времени: онлайн-регистрация, выезд в аэропорт, заселение,
  дедлайн бесплатной отмены, выезд из отеля. После повторного оформления — `previous_order_id`, `price_difference_rub`
- `trace` — панель «Как агент думает» (шаги и время)
- `diff` — подсветка «было → стало» после пересборки; `field`: destination, dates, outbound, return, hotel, total

## 3. Метрики (роль A ↔ роль D)
`python evals/run.py [--offline]` — запросы в `evals/queries.jsonl`, роль D пополняет до 50.
Формат строки: `{"id": "...", "text": "...", "expect": {...}}`. Итог: completion, clarify_avg,
first_rec_s_median, stitch_err, explained, correct.

`expect` необязателен, но без него сценарий проверяет только «дошёл до брони», а не «понял правильно».
Ключи: `top_dest_in`, `top_dest_not`, `dest_not` (id направлений), `children`, `origin`, `destination`,
`check_in_mmdd`, `first_message_contains` — подробности в начале `evals/run.py`.
`correct` — доля сценариев с `expect`, где все ожидания сбылись.
