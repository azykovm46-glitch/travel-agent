"""Известные места, которых может не быть в каталоге бэкенда: страна, регион и тип отдыха.

Это не данные о ценах, рейсах или погоде — только чтобы честно сказать «по этому месту
данных нет» и подобрать ближайшее из того, что есть: сначала та же страна, потом тот же
регион и похожий отдых. Агент не придумывает направление, которого нет в бэкенде.
"""
from __future__ import annotations

import re

TRIP_LABEL = {"sea": "море", "city": "город", "culture": "культура", "nature": "природа",
              "ski": "горные лыжи", "food": "гастрономия"}

# Страна -> регион: «рядом» для подбора замены
REGIONS = {
    "Азия": ["Китай", "Япония", "Таиланд", "Вьетнам", "Индонезия", "Шри-Ланка", "Индия", "Мальдивы", "Малайзия"],
    "Ближний Восток": ["ОАЭ", "Оман", "Египет", "Катар", "Иордания"],
    "Средиземноморье": ["Турция", "Греция", "Кипр", "Испания", "Италия", "Черногория", "Франция"],
    "Европа": ["Чехия", "Германия", "Австрия", "Венгрия", "Сербия"],
    "Россия и соседи": ["Россия", "Беларусь", "Грузия", "Армения", "Азербайджан", "Абхазия", "Казахстан", "Узбекистан"],
}
_REGION_OF = {c: r for r, countries in REGIONS.items() for c in countries}

PLACES: list[dict] = [
    {"name": "Стамбул", "country": "Турция", "tags": ["city", "culture", "food"], "patterns": [r"\bстамбул"]},
    {"name": "Каппадокия", "country": "Турция", "tags": ["nature", "culture"], "patterns": [r"\bкаппадоки"]},
    {"name": "Пхукет", "country": "Таиланд", "tags": ["sea", "nature"], "patterns": [r"\bпхукет", r"\bта[йи]ланд"]},
    {"name": "Бали", "country": "Индонезия", "tags": ["sea", "nature"], "patterns": [r"\bбали\b"]},
    {"name": "Нячанг", "country": "Вьетнам", "tags": ["sea"], "patterns": [r"\bнячанг", r"\bвьетнам"]},
    {"name": "Мальдивы", "country": "Мальдивы", "tags": ["sea"], "patterns": [r"\bмальдив"]},
    {"name": "Шри-Ланка", "country": "Шри-Ланка", "tags": ["sea", "nature"], "patterns": [r"\bшри[- ]?ланк"]},
    {"name": "Гоа", "country": "Индия", "tags": ["sea"], "patterns": [r"\bгоа\b"]},
    {"name": "Кипр", "country": "Кипр", "tags": ["sea"], "patterns": [r"\bкипр"]},
    {"name": "Черногория", "country": "Черногория", "tags": ["sea", "nature"], "patterns": [r"\bчерногори"]},
    {"name": "Париж", "country": "Франция", "tags": ["city", "culture", "food"], "patterns": [r"\bпариж"]},
    {"name": "Прага", "country": "Чехия", "tags": ["city", "culture"], "patterns": [r"\bпраг[аеуи]\b"]},
    {"name": "Тбилиси", "country": "Грузия", "tags": ["city", "culture", "food"], "patterns": [r"\bтбилиси", r"\bгрузи"]},
    {"name": "Ереван", "country": "Армения", "tags": ["city", "culture", "food"], "patterns": [r"\bереван", r"\bармени"]},
    {"name": "Баку", "country": "Азербайджан", "tags": ["city", "food", "sea"], "patterns": [r"\bбаку\b", r"\bазербайджан"]},
    {"name": "Казань", "country": "Россия", "tags": ["city", "culture"], "patterns": [r"\bказан[ьи]"]},
    {"name": "Алтай", "country": "Россия", "tags": ["nature"], "patterns": [r"\bалта[йюе]"]},
    {"name": "Байкал", "country": "Россия", "tags": ["nature"], "patterns": [r"\bбайкал"]},
    {"name": "Шерегеш", "country": "Россия", "tags": ["ski", "nature"], "patterns": [r"\bшерегеш"]},
    {"name": "Красная Поляна", "country": "Россия", "tags": ["ski", "nature"],
     "patterns": [r"\bкрасн\w* полян", r"\bроз\w* хутор"]},
    {"name": "Домбай", "country": "Россия", "tags": ["ski", "nature"], "patterns": [r"\bдомба[йею]"]},
]


def lookup_place(text: str | None) -> dict | None:
    t = (text or "").lower().replace("ё", "е")
    return next((p for p in PLACES if any(re.search(pat, t) for pat in p["patterns"])), None)


def region_of(country: str | None) -> str | None:
    return _REGION_OF.get(country or "")


def tags_label(tags: list[str]) -> str:
    return ", ".join(TRIP_LABEL[t] for t in tags if t in TRIP_LABEL)
