"""Нормализация текста до правил: простой английский и опечатки в гласных.

Нужна офлайн-режиму (резерв на защите): LLM такие запросы понимает сама,
а правила без этого видят «дикабре» и «beach» как незнакомые слова.

Опечатки исправляем узко: слово меняется на словарное, только если отличается
от него одними гласными на тех же местах («хачу» → «хочу», «масквы» → «москвы»).
Так «марсе» не станет «марте», а «горы» — «морем»: там различаются согласные.
"""
from __future__ import annotations

import re

VOWELS = set("аеиоуыэюяё")
VOCAB = set("""
январь января январе февраль февраля феврале март марта марте апрель апреля апреле
июнь июня июне июль июля июле август августа августе сентябрь сентября сентябре
октябрь октября октябре ноябрь ноября ноябре декабрь декабря декабре
море моря морю морем пляж пляжа пляжу отдохнуть отдых отпуск поехать хочу хотим
недорого дешево дешевле бюджетно вдвоем втроем женой мужем девушкой ребенком ребенок детьми детей
собакой кошкой неделю ночей ночи выходные пересадок перелетов включено завтраками лыжах лыжи горы
москвы петербурга питера казани екатеринбурга новосибирска самары
сочи анталья анталью турцию турции египет дубай стамбул пхукет таиланд калининград
""".split())
# похожи на словарные одними гласными, но это другие слова
NOT_TYPOS = {"мере", "мира", "мире", "пора", "поры", "мору", "доча"}

EN_CITY_FROM = {"moscow": "москвы", "st petersburg": "петербурга", "st. petersburg": "петербурга",
                "saint petersburg": "петербурга", "petersburg": "петербурга", "kazan": "казани",
                "yekaterinburg": "екатеринбурга", "ekaterinburg": "екатеринбурга", "novosibirsk": "новосибирска",
                "samara": "самары", "vladivostok": "владивостока"}
EN_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
             "october", "november", "december"]
RU_MONTHS_IN = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре",
                "октябре", "ноябре", "декабре"]
EN_RU = [
    (r"\bwith (?:a |my |our )?(\d{1,2})[ -]years?[ -]old (?:kid|child|son|daughter|boy|girl)\b", r"с ребенком \1 лет"),
    (r"\b(?:a |my |our )?(\d{1,2})[ -]years?[ -]old (?:kid|child|son|daughter|boy|girl)\b", r"с ребенком \1 лет"),
    (r"\bwith (?:a |my |our )?(?:kid|child|son|daughter|baby)\b", "с ребенком"),
    (r"\bwith (?:the |our |my )?(?:kids|children)\b", "с детьми"),
    (r"\b(\d) (?:kids|children)\b", r"\1 детей"),
    (r"\b(\d+) adults?\b", r"\1 взрослых"),
    (r"\b(?:for two|the two of us|two of us|as a couple|with my (?:wife|husband|girlfriend|boyfriend|partner))\b", "вдвоем"),
    (r"\bfor three\b", "втроем"),
    (r"\b(?:alone|solo|by myself|just me)\b", "один"),
    (r"\b(\d+) nights?\b", r"на \1 ночей"),
    (r"\b(?:for )?two weeks\b", "на 2 недели"),
    (r"\b(?:for )?(?:a|one) week\b", "на неделю"),
    (r"\b(?:for the |a |on the )?weekend\b", "на выходные"),
    (r"\b(?:beach|seaside|sea)\b", "море"),
    (r"\b(?:skiing|ski)\b", "лыжи"),
    (r"\bmountains?\b", "горы"),
    (r"\b(?:city break|sightseeing|museums?)\b", "город экскурсии"),
    (r"\b(?:cheap|budget|inexpensive|affordable|low cost)\b", "недорого"),
    (r"\b(?:luxury|premium|5[- ]star)\b", "люкс"),
    (r"\b(?:direct(?: flights?)?|non-?stop|no (?:layovers|connections|stopovers))\b", "без пересадок"),
    (r"\bby train\b", "поездом"),
    (r"\bwith (?:a |my |our )?dog\b", "с собакой"),
    (r"\bwith (?:a |my |our )?(?:cat|pet)\b", "с питомцем"),
    (r"\bbreakfast\b", "завтрак"),
    (r"\bfree cancellation\b", "бесплатная отмена"),
    (r"\b(?:book it|let'?s book|checkout|check out|i'?ll take it)\b", "оформляем"),
    (r"\b(?:antalya|turkey)\b", "анталья"), (r"\bsochi\b", "сочи"), (r"\bdubai\b", "дубай"),
    (r"\bistanbul\b", "стамбул"), (r"\b(?:phuket|thailand)\b", "пхукет"), (r"\b(?:egypt|sharm(?: el sheikh)?)\b", "шарм"),
    (r"\bbaku\b", "баку"), (r"\bkaliningrad\b", "калининград"),
]


def english_to_russian(t: str) -> str:
    """Перевести ключевые английские слова в русские, которые понимают правила."""
    if not re.search(r"[a-z]{3,}", t):
        return t
    for en, ru in EN_CITY_FROM.items():
        t = re.sub(rf"\bfrom {re.escape(en)}\b", f"из {ru}", t)
    t = re.sub(r"\bkazan\b", "казань", t)                      # Казань как направление
    t = re.sub(r"\b(?:st\.? |saint )?petersburg\b", "петербург", t)
    for i, m in enumerate(EN_MONTHS):
        t = re.sub(rf"\b{m}\b", RU_MONTHS_IN[i], t)
    for pat, ru in EN_RU:
        t = re.sub(pat, ru, t)
    return t


def _vowel_typo(word: str, target: str) -> int | None:
    """Сколько гласных надо заменить, чтобы word стал target; None — если отличаются не только гласные."""
    if len(word) != len(target):
        return None
    diff = 0
    for a, b in zip(word, target):
        if a != b:
            if a not in VOWELS or b not in VOWELS:
                return None
            diff += 1
    return diff


def fix_typos(t: str) -> tuple[str, list[str]]:
    """Исправить опечатки в гласных. Возвращает текст и список исправлений «было→стало»."""
    fixes = []

    def repl(m: re.Match) -> str:
        word = m.group(0)
        if len(word) < 4 or word in VOCAB or word in NOT_TYPOS:
            return word
        limit = 1 if len(word) <= 5 else 2
        best = min(((d, v) for v in VOCAB if (d := _vowel_typo(word, v)) is not None and d <= limit), default=None)
        if not best:
            return word
        fixes.append(f"{word}→{best[1]}")
        return best[1]

    return re.sub(r"[а-я]+", repl, t), fixes
