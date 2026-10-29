"""Именование ресурсов источника: контейнер, том, префикс.

Чистые функции без Docker SDK: нужны и общему lifecycle, и vendor-модулям
(рабочее подключение резолвится по имени контейнера), поэтому живут отдельно
от container_manager. Префикс читается из constants в момент вызова.
"""
from __future__ import annotations

import re

from . import constants
from .models import SourceConfig


def sanitize_user_token(token: str | None) -> str:
    """Очищает пользовательский токен (префикс или имя инстанса) от пробелов, краевых дефисов и спецсимволов."""
    if not token:
        return ""
    # Заменяем недопустимые спецсимволы и пробелы на дефис
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(token).strip())
    # Убираем дефисы, точки и подчеркивания по краям и приводим к нижнему регистру
    return cleaned.strip("-._").lower()


def container_name_for(config: SourceConfig) -> str:
    prefix = sanitize_user_token(constants.CONTAINER_PREFIX)
    src_name = sanitize_user_token(config.name)
    parts: list[str] = []
    if prefix.startswith("sydafa"):
        parts.append(prefix)
    else:
        parts.append("sydafa")
        if prefix:
            parts.append(prefix)
    parts.extend(["src", config.type, src_name])
    return "-".join(parts)


def volume_name_for(config: SourceConfig) -> str:
    prefix = sanitize_user_token(constants.CONTAINER_PREFIX)
    src_name = sanitize_user_token(config.name)
    parts: list[str] = []
    if prefix.startswith("sydafa"):
        parts.append(prefix)
    else:
        parts.append("sydafa")
        if prefix:
            parts.append(prefix)
    parts.extend(["data", config.type, src_name])
    return "-".join(parts)
