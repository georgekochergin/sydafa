"""Однократное применение init.json к пустому источнику."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import TypeAdapter

from .models import ExecOperation
from .schema_state import get_live_schema
from .sources import source_names

logger = logging.getLogger("sydafa.init_apply")


def find_init_file() -> Path | None:
    env_path = os.environ.get("SYDAFA_INIT_FILE")
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    candidates = [
        Path("/app/config/init.json"),
        Path("config/init.json"),
        Path("sydafa/init.json"),
        Path("conf/sydafa/init.json"),
        Path("test/sydafa/init.json"),
        Path(__file__).parent.parent / "config" / "init.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_init_ops() -> dict:
    init_path = find_init_file()
    if not init_path:
        return {}
    try:
        content = json.loads(init_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Ошибка чтения init-файла %s: %s", init_path, e)
        return {}
    if not isinstance(content, dict):
        return {}
    return content.get("sources", content) if isinstance(content, dict) else {}


def is_initialized(source: str) -> bool:
    live = get_live_schema(source)
    return bool(live.schemas) or bool(live.tables)


def apply_init_for_source(source: str) -> None:
    if is_initialized(source):
        logger.info("Источник '%s' уже инициализирован, init.json пропускается.", source)
        return

    sources_dict = load_init_ops()
    if source not in sources_dict or not isinstance(sources_dict[source], list):
        logger.info("Источник '%s' отсутствует в init-файле, оставляем чистую базу.", source)
        return

    raw_ops = sources_dict[source]
    if not raw_ops:
        return

    from . import exec_engine

    adapter = TypeAdapter(list[ExecOperation])
    try:
        ops = adapter.validate_python(raw_ops)
        res = exec_engine.execute_source_operations(source, ops)
        logger.info("Источник '%s': выполнено %d операций из init-файла", source, res.executed)
    except Exception as e:
        logger.error("Ошибка применения init-файла для источника '%s': %s", source, e, exc_info=True)


def apply_init_all_loaded() -> None:
    for src in source_names():
        apply_init_for_source(src)
