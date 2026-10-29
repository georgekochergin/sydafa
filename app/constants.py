"""Общие константы сервиса.

Вендорные справочники (порты, образы, пути данных) живут в runtime-провайдерах
соответствующих backend (`app/backends/rdbms/<engine>/runtime.py`) и не являются
здесь конкурирующим реестром типов.
"""
from __future__ import annotations

import os
from pathlib import Path

SYDAFA_DB = "sydafa"
SYDAFA_USER = "sydafa"
READER_USER = "sydafa"
READER_PASSWORD = "sydafa"

CONFIG_SOURCES_DIR = Path(os.environ.get("CONFIG_SOURCES_DIR", "/app/config/sources"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))

CONTAINER_PREFIX = os.environ.get("SYDAFA_CONTAINER_PREFIX", "")
MAX_TTL_MINUTES = int(os.environ.get("SYDAFA_MAX_TTL_MINUTES", "30"))
DEFAULT_TTL_MINUTES = int(os.environ.get("SYDAFA_TTL_MINUTES", "15"))
IDLE_WATCH_SECONDS = int(os.environ.get("SYDAFA_IDLE_WATCH_SECONDS", "10"))
