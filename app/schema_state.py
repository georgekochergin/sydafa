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


def get_next_batch_id(source: str) -> int:
    """Атомарно выделяет следующий целочисленный идентификатор батча/инкремента для источника."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    seq_file = STATE_DIR / f"batch_sequence.{source}.json"
    current_val = 0
    if seq_file.exists():
        try:
            data = json.loads(seq_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                current_val = int(data.get("current_batch_id", 0))
        except Exception as e:
            logger.warning("Не удалось прочитать batch_sequence для '%s': %s", source, e)
    next_val = current_val + 1
    seq_file.write_text(json.dumps({"current_batch_id": next_val}, indent=2) + "\n", encoding="utf-8")
    return next_val


def reset_batch_sequence(source: str) -> None:
    """Сбрасывает сиквенс батчей для указанного источника."""
    seq_file = STATE_DIR / f"batch_sequence.{source}.json"
    if seq_file.exists():
        try:
            seq_file.unlink()
        except Exception:
            pass


def reset_memory_cache(source: str) -> None:
    global _live_schemas
    if source in _live_schemas:
        del _live_schemas[source]
    log_file = STATE_DIR / f"schema_evolution_log.{source}.json"
    if log_file.exists():
        try:
            log_file.unlink()
        except Exception:
            pass
    reset_batch_sequence(source)
