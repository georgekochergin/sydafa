"""Аддитивный Seed данных и схем из init.json."""
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


def get_ops_for_source(sources_dict: dict, source_name: str) -> list[dict]:
    val = sources_dict.get(source_name)
    if isinstance(val, dict):
        ops = val.get("operations", [])
        return ops if isinstance(ops, list) else []
    elif isinstance(val, list):
        return val
    return []


def is_initialized(source: str) -> bool:
    live = get_live_schema(source)
    return bool(live.schemas) or bool(live.tables)


def apply_init_for_source(source: str) -> None:
    sources_dict = load_init_ops()
    raw_ops = get_ops_for_source(sources_dict, source)
    if not raw_ops:
        logger.info("Для источника '%s' нет операций в init-файле (чистая база).", source)
        return

    live = get_live_schema(source)
    was_initialized = bool(live.schemas) or bool(live.tables)

    # Проверка исчезнувших таблиц (информационный лог без DROP)
    init_tables = {
        f"{op.get('schema')}.{op.get('name')}"
        for op in raw_ops
        if isinstance(op, dict) and op.get("op") == "create_table"
    }
    missing_in_init = set(live.tables.keys()) - init_tables
    for tbl in sorted(missing_in_init):
        logger.info(
            "Таблица '%s' присутствует в базе '%s', но отсутствует в текущем init.json. Данные сохранены.",
            tbl,
            source,
        )

    # Аддитивная фильтрация операций
    filtered_ops: list[dict] = []
    for op in raw_ops:
        if not isinstance(op, dict):
            continue
        op_type = op.get("op")

        if op_type == "create_schema":
            name = op.get("name")
            if name in live.schemas:
                continue

        elif op_type == "create_table":
            key = f"{op.get('schema')}.{op.get('name')}"
            if key in live.tables:
                continue

        elif op_type == "add_column":
            tbl_key = f"{op.get('schema')}.{op.get('table')}"
            col_name = (op.get("column") or {}).get("name")
            if tbl_key in live.tables:
                existing_cols = [c.name for c in live.tables[tbl_key].columns]
                if col_name in existing_cols:
                    continue

        elif was_initialized and op_type in ("append", "truncate", "update", "delete"):
            # Повторный запуск: не дублировать начальный Seed DML
            continue

        filtered_ops.append(op)

    if not filtered_ops:
        logger.info("Источник '%s' уже актуален, новые операции в init.json отсутствуют.", source)
        return

    from . import exec_engine

    adapter = TypeAdapter(list[ExecOperation])
    try:
        ops = adapter.validate_python(filtered_ops)
        res = exec_engine.execute_source_operations(source, ops)
        logger.info("Источник '%s': выполнено %d операций из init-файла", source, res.executed)
    except Exception as e:
        logger.error("Ошибка применения init-файла для источника '%s': %s", source, e, exc_info=True)


def apply_init_all_loaded() -> None:
    for src in source_names():
        apply_init_for_source(src)
