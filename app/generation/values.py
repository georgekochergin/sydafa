"""Чистые генераторы значений: Faker, шаблоны, даты, choice, sequence (явный поставщик).

Здесь нет SQL, драйверов, Docker, FastAPI и обращений к /state: для kind=sequence
значение приходит через явно переданный поставщик (уже выделенный диапазон/счётчик).
"""
from __future__ import annotations

import calendar
import random
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from faker import Faker

from ..models import ColumnSpec, GeneratorSpec

_faker_cache: dict[str, Faker] = {}

# Поставщик значений sequence: имя колонки -> следующее монотонное значение.
SequenceSupplier = Callable[[str], int]


class GeneratorError(ValueError):
    pass


def _get_faker(locale: str) -> Faker:
    if locale not in _faker_cache:
        _faker_cache[locale] = Faker(locale)
    return _faker_cache[locale]


_PATTERN_DIGITS = "0123456789"
_PATTERN_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PATTERN_LOWER = "abcdefghijklmnopqrstuvwxyz"
_PATTERN_ALNUM = _PATTERN_DIGITS + _PATTERN_UPPER + _PATTERN_LOWER
_PATTERN_CLASSES = {
    "#": _PATTERN_DIGITS,
    "@": _PATTERN_UPPER,
    "a": _PATTERN_LOWER,
    "?": _PATTERN_ALNUM,
}


def render_pattern(template: str) -> str:
    """Подставляет случайные символы вместо плейсхолдеров шаблона, литералы переносит как есть."""
    return "".join(
        random.choice(_PATTERN_CLASSES[ch]) if ch in _PATTERN_CLASSES else ch
        for ch in template
    )


def resolve_anchor_date(anchor: str) -> date:
    today = date.today()
    if anchor == "start_of_year":
        return date(today.year, 1, 1)
    if anchor == "end_of_year":
        return date(today.year, 12, 31)
    if anchor == "today":
        return today
    if anchor == "start_of_month":
        return date(today.year, today.month, 1)
    if anchor == "start_of_quarter":
        q_month = ((today.month - 1) // 3) * 3 + 1
        return date(today.year, q_month, 1)
    raise GeneratorError(f"Неизвестная опорная точка anchor: '{anchor}'")


def shift_date(base: date, dim: str, offset: int) -> date:
    if dim == "d":
        return base + timedelta(days=offset)
    if dim == "m":
        total_months = base.year * 12 + (base.month - 1) + offset
        target_year = total_months // 12
        target_month = (total_months % 12) + 1
        max_days = calendar.monthrange(target_year, target_month)[1]
        target_day = min(base.day, max_days)
        return date(target_year, target_month, target_day)
    if dim == "y":
        target_year = base.year + offset
        if base.month == 2 and base.day == 29:
            target_day = 29 if calendar.isleap(target_year) else 28
        else:
            target_day = base.day
        return date(target_year, base.month, target_day)
    raise GeneratorError(f"Неизвестная размерность dim: '{dim}'. Разрешены 'y', 'm', 'd'")


def generate_value(
    col: ColumnSpec | str,
    row_values: dict[str, Any] | None = None,
    gen: GeneratorSpec | None = None,
    col_type: str | None = None,
    sequence: SequenceSupplier | None = None,
) -> Any:
    """Генерирует значение колонки: явный генератор, тип-колонки или поставщик sequence."""
    if isinstance(col, ColumnSpec):
        col_name = col.name
        col_type = col.type
        if gen is None:
            gen = getattr(col, "generator", None)
    else:
        col_name = str(col)
        col_type = col_type or "string"

    # Если явный генератор не задан, генерируем синтетическое значение по типу колонки
    if gen is None:
        t_lower = col_type.lower()
        if "uuid" in t_lower:
            return str(uuid.uuid4())
        if "int" in t_lower:
            return random.randint(1, 10000)
        if "decimal" in t_lower or "numeric" in t_lower or "float" in t_lower:
            return round(random.uniform(10.0, 5000.0), 2)
        if "date" in t_lower or "time" in t_lower:
            today = date.today()
            return today - timedelta(days=random.randint(0, 365))
        if "char" in t_lower or "str" in t_lower or "text" in t_lower:
            return _get_faker("ru_RU").word()
        return None

    null_ratio = getattr(gen, "null_ratio", None)
    if null_ratio is not None and random.random() < null_ratio:
        return None

    kind = getattr(gen, "kind", None)
    if kind == "faker":
        locale = getattr(gen, "locale", "ru_RU")
        provider = getattr(gen, "provider", "word")
        fk = _get_faker(locale)
        func = getattr(fk, provider, None)
        if func is None:
            func = fk.word
        return func()

    if kind == "pattern":
        template = getattr(gen, "template", None)
        if not template:
            raise GeneratorError("Для генератора pattern не указан обязательный параметр template")
        return render_pattern(template)

    if kind == "choice":
        values = getattr(gen, "values", [])
        if not values:
            return None
        weights = getattr(gen, "weights", None)
        if weights:
            return random.choices(values, weights=weights, k=1)[0]
        return random.choice(values)

    if kind == "random_decimal":
        min_v = getattr(gen, "min", 0.0)
        max_v = getattr(gen, "max", 100000.0)
        return round(random.uniform(min_v, max_v), 2)

    if kind == "random_date":
        anchor = getattr(gen, "anchor", "start_of_year")
        dim = getattr(gen, "dim", "d")
        min_offset = getattr(gen, "min_offset", None)
        max_offset = getattr(gen, "max_offset", None)

        if min_offset is None and max_offset is None:
            if anchor == "start_of_year" and dim == "d":
                year = date.today().year
                start = date(year, 1, 1)
                end = date(year, 12, 31)
                days = (end - start).days
                return start + timedelta(days=random.randint(0, days))
            min_o, max_o = 0, 0
        else:
            min_o = min_offset if min_offset is not None else 0
            max_o = max_offset if max_offset is not None else min_o

        if min_o > max_o:
            min_o, max_o = max_o, min_o

        offset = random.randint(min_o, max_o)
        base = resolve_anchor_date(anchor)
        return shift_date(base, dim, offset)

    if kind == "date_offset":
        ref_column = getattr(gen, "ref_column", None)
        if not ref_column:
            raise GeneratorError(f"Для генератора date_offset не указан обязательный параметр ref_column")

        base_val = row_values.get(ref_column) if row_values is not None else None
        if base_val is None:
            return None

        if isinstance(base_val, str):
            base_date = date.fromisoformat(base_val)
        elif isinstance(base_val, datetime):
            base_date = base_val.date()
        elif isinstance(base_val, date):
            base_date = base_val
        else:
            raise GeneratorError(f"Значение колонки '{ref_column}' ({base_val}) не является корректной датой")

        dim = getattr(gen, "dim", "y")
        min_o = getattr(gen, "min_offset", 1)
        max_o = getattr(gen, "max_offset", 5)
        if min_o > max_o:
            min_o, max_o = max_o, min_o

        offset = random.randint(min_o, max_o)
        return shift_date(base_date, dim, offset)

    if kind == "sequence":
        if sequence is None:
            raise GeneratorError(
                f"Для генератора sequence колонки '{col_name}' не передан поставщик значений"
            )
        return sequence(col_name)

    if kind == "uuid":
        return str(uuid.uuid4())

    return None
