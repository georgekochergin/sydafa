"""Управление live-состоянием схемы данных для каждого источника."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .constants import STATE_DIR
from .models import LiveSchema, TableSchema

logger = logging.getLogger("sydafa.schema_state")

_live_schemas: dict[str, LiveSchema] = {}


def get_live_schema(source: str) -> LiveSchema:
    if source not in _live_schemas:
        load_live_schema(source)
    return _live_schemas[source]


def load_live_schema(source: str) -> LiveSchema:
    global _live_schemas
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"live_schema.{source}.json"

    if state_file.exists():
        try:
            raw = json.loads(state_file.read_text(encoding="utf-8"))
            live = LiveSchema(**raw)
            _live_schemas[source] = live
            return live
        except Exception as e:
            logger.warning("Не удалось прочитать live_schema для '%s': %s. Создаётся пустое состояние.", source, e)

    live = LiveSchema(schemas=[], tables={})
    _live_schemas[source] = live
    return live


def save_live_schema(source: str, live: LiveSchema) -> None:
    global _live_schemas
    _live_schemas[source] = live
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"live_schema.{source}.json"
    state_file.write_text(live.model_dump_json(indent=2, by_alias=True), encoding="utf-8")


def append_evolution_log(source: str, entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_file = STATE_DIR / f"schema_evolution_log.{source}.json"
    logs = []
    if log_file.exists():
        try:
            logs = json.loads(log_file.read_text(encoding="utf-8"))
        except Exception:
            logs = []
    logs.append(entry)
    log_file.write_text(json.dumps(logs, indent=2, ensure_ascii=False), encoding="utf-8")
