"""Константы и реестр образов сервиса sydafa."""
from __future__ import annotations

import os
from pathlib import Path

# Базовые параметры технического суперпользователя
SYDAFA_DB = "sydafa"
SYDAFA_USER = "sydafa"
SYDAFA_PASSWORD = "sydafa"

# Пути к директориям конфигурации и состояния
CONFIG_SOURCES_DIR = Path(os.environ.get("CONFIG_SOURCES_DIR", "/app/config/sources"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))

# Внутренний реестр Docker-образов для СУБД источников
SOURCE_IMAGES: dict[str, str] = {
    "mssql": os.environ.get(
        "SYDAFA_MSSQL_IMAGE",
        "mcr.microsoft.com/mssql/server:2022-CU27-ubuntu-22.04",
    ),
    "oracle": os.environ.get(
        "SYDAFA_ORACLE_IMAGE",
        "gvenzl/oracle-free:23-slim-faststart",
    ),
}
