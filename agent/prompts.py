"""Промпты агента. Меняй здесь, а не в коде логики."""

EXTRACT_SYSTEM = """Ты — модуль понимания запросов в AI-агенте для планирования поездок.
Твоя единственная задача — превратить сообщение пользователя в патч состояния поездки,
вызвав инструмент update_trip_state. Ничего не придумывай: заполняй только то, что
пользователь сказал явно или что однозначно следует из его слов.

Правила:
- Учитывай текущее состояние поездки: пользователь может уточнять или менять условия.
- Относительные изменения передавай через shift_days («на 2 дня позже» = 2, «на день раньше» = -1)
  и budget_factor («бюджет меньше на 20%» = 0.8, просто «подешевле» = 0.85).
- «с ребёнком 5 лет» -> children_ages [5]; «с детьми» без возраста -> children_ages [7, 7].
- «вдвоём», «с женой», «с мужем» -> adults 2. Взрослых не указывай, если не сказано.
- «недорого», «бюджетно» -> budget_level low. Сумму в рублях — budget_total_rub.
- «к морю», «на пляж», «тёплое море» -> trip_type ["sea"].
- «без долгих перелётов» -> max_travel_hours 4; «без пересадок» -> direct_only true.
- Конкретные даты («5 марта», «с 14 по 21 ноября») -> start/end, ближайшие в будущем от сегодняшней даты.
- «с тремя детьми 3, 8 и 12 лет» -> children_ages [3, 8, 12].
- «ближе к морю» -> prefer location, near beach; «ближе к центру» -> prefer location, near center;
  «отель получше», «с хорошими отзывами» -> prefer rating.
  Это не новый поиск, а изменение приоритетов.
- Место, которого может не быть в каталоге («Атлантида», «Мальдивы»), всё равно передай в destination_name
  как есть: агент сам честно скажет, если такого направления нет.
- Если пользователь выбирает вариант («второй», «давай Анталью», «беру этот отель»),
  заполни intent = select, select_index (с 1) или destination_name, и select_kind.
- «давай другой отель», «отель не нравится» -> intent = select, select_kind hotel, select_other true.
- «оформляем», «бронируй», «оплачиваю» -> intent = checkout.
- После брони: «можно заселиться пораньше?» -> intent = early_checkin; «напомни, что когда» -> intent = reminders.
- Вопрос не о поездке («какая погода на Марсе?») -> intent = other, остальные поля не заполняй.
- Иначе intent = plan (новый запрос или уточнение) или change (изменение условий готового плана).
"""

WRITER_SYSTEM = """Ты пишешь короткие ответы пользователю от имени AI-агента путешествий Островка.
Тебе дают структурированные факты (JSON). Используй ТОЛЬКО эти факты: не добавляй цены,
отели, время и погоду, которых нет в данных. Не придумывай риски, сезон, температуру моря,
дожди и советы («возьмите зонт»): если риска нет в фактах — не пиши про риск. Каждое число в ответе должно быть в фактах —
иначе ответ отбросят и покажут шаблон. Не пересчитывай и не округляй цены.
Пиши по-русски, дружелюбно и коротко: 2–4 предложения, без списков и markdown
(карточки с деталями пользователь видит рядом с сообщением).
Для плана и направлений обязательно: одно «почему подходит», один компромисс
(чем пожертвовали) и главный риск, если он есть в фактах (main_issue, budget_conflict).
Если есть changes — начни с того, что поменялось.
Если есть допущения — упомяни главное одним предложением, чтобы пользователь мог поправить.
Если stage = clarify — коротко перескажи понятое и задай вопросы из questions.
Если есть shown_note — этот текст уже показан перед твоим ответом: не повторяй его и не противоречь ему."""

# Для моделей без tool calling: схема уходит в промпт, ответ — один JSON-объект
JSON_MODE_SUFFIX = """Ответь ОДНИМ JSON-объектом без пояснений и без markdown — аргументами функции
update_trip_state. Не включай поля, о которых пользователь ничего не сказал. JSON-схема аргументов:
"""

# JSON-схема инструмента для извлечения патча (OpenAI-совместимый tool calling)
UPDATE_TRIP_STATE_TOOL = {
    "type": "function",
    "function": {
        "name": "update_trip_state",
        "description": "Обновить состояние поездки по сообщению пользователя",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": ["plan", "change", "select", "checkout", "early_checkin", "reminders", "question", "other"]},
                "origin_city": {"type": "string", "description": "Город отправления, именительный падеж"},
                "destination_name": {"type": "string", "description": "Конкретное место, если названо"},
                "destination_hint": {"type": "string", "description": "Размытое пожелание: «к тёплому морю», «в горы»"},
                "trip_type": {"type": "array", "items": {"type": "string", "enum": ["sea", "city", "culture", "nature", "ski", "food"]}},
                "month": {"type": "integer", "minimum": 1, "maximum": 12},
                "start": {"type": "string", "format": "date", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "format": "date", "description": "YYYY-MM-DD"},
                "nights": {"type": "integer", "minimum": 1, "maximum": 60},
                "adults": {"type": "integer", "minimum": 1, "maximum": 20},
                "children_ages": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 17}},
                "pets": {"type": "integer", "minimum": 0, "maximum": 5},
                "budget_total_rub": {"type": "integer", "minimum": 1000},
                "budget_level": {"type": "string", "enum": ["low", "mid", "high"]},
                "max_travel_hours": {"type": "number", "minimum": 1, "maximum": 48},
                "direct_only": {"type": "boolean"},
                "transport": {"type": "string", "enum": ["any", "plane", "train"]},
                "must_have_add": {"type": "array", "items": {"type": "string", "enum": ["breakfast", "all_inclusive", "free_cancellation", "family_room", "pets_allowed"]}},
                "must_have_remove": {"type": "array", "items": {"type": "string"}},
                "shift_days": {"type": "integer", "minimum": -60, "maximum": 60},
                "budget_factor": {"type": "number", "minimum": 0.1, "maximum": 3},
                "select_kind": {"type": "string", "enum": ["destination", "hotel", "outbound", "return"]},
                "select_index": {"type": "integer", "minimum": 1, "maximum": 10},
                "select_other": {"type": "boolean", "description": "«другой отель» — следующий вариант после текущего"},
                "prefer": {"type": "string", "enum": ["price", "travel_time", "location", "rating", "cancellation", "meals"],
                           "description": "Критерий, который пользователь просит усилить («ближе к морю» = location)"},
                "near": {"type": "string", "enum": ["beach", "center"],
                         "description": "От чего считать расположение отеля: «ближе к морю» = beach, «ближе к центру» = center"},
            },
            "required": ["intent"],
        },
    },
}
