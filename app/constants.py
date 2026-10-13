"""Константы и реестр образов сервиса sydafa."""
from __future__ import annotations

import os
from pathlib import Path

SYDAFA_DB = "sydafa"
SYDAFA_USER = "sydafa"

CONFIG_SOURCES_DIR = Path(os.environ.get("CONFIG_SOURCES_DIR", "/app/config/sources"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))

CONTAINER_PREFIX = os.environ.get("SYDAFA_CONTAINER_PREFIX", "")
DEFAULT_TTL_MINUTES = int(os.environ.get("SYDAFA_TTL_MINUTES", "15"))
IDLE_WATCH_SECONDS = int(os.environ.get("SYDAFA_IDLE_WATCH_SECONDS", "10"))

DATA_VOLUME_PATHS: dict[str, str] = {
    "mssql": "/var/opt/mssql",
    "oracle": "/opt/oracle/oradata",
}

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
