"""Управление live-состоянием схемы данных для каждого источника."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .constants import STATE_DIR
from .models import LiveSchema, TableSchema

logger = logging.getLogger("sydafa.schema_state")

_live_schemas: dict[str, LiveSchema] = {}
_sequences_cache: dict[str, dict[str, int]] = {}


def _sequences_file(source: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"sequences.{source}.json"


def load_sequences(source: str) -> dict[str, int]:
    """Загружает словарь счетчиков сиквенсов для указанного источника."""
    global _sequences_cache
    if source in _sequences_cache:
        return _sequences_cache[source]

    sf = _sequences_file(source)
    if sf.exists():
        try:
            raw = json.loads(sf.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                seqs = {k: int(v) for k, v in raw.items()}
                _sequences_cache[source] = seqs
                return seqs
        except Exception as e:
            logger.warning("Не удалось прочитать sequences для '%s': %s", source, e)

    _sequences_cache[source] = {}
    return _sequences_cache[source]


def save_sequences(source: str, seqs: dict[str, int]) -> None:
    """Сохраняет словарь счетчиков сиквенсов на диск."""
    global _sequences_cache
    _sequences_cache[source] = seqs
    sf = _sequences_file(source)
    sf.write_text(json.dumps(seqs, indent=2) + "\n", encoding="utf-8")


def reserve_sequence_range(source: str, entity_fqn: str, column: str, count: int) -> int:
    """Атомарно выделяет непрерывный диапазон из count идентификаторов для колонки сущности.

    Возвращает начальное значение диапазона (start_val). Выделенный диапазон: [start_val, start_val + count - 1].
    Счетчик стартует с 0 (первое выданное значение 1) и является строго монотонным.
    """
    if count <= 0:
        count = 1
    seqs = load_sequences(source)
    key = f"{entity_fqn}.{column}"
    current_val = seqs.get(key, 0)
    start_val = current_val + 1
    new_max = current_val + count
    seqs[key] = new_max
    save_sequences(source, seqs)
    return start_val


def get_current_sequence_value(source: str, entity_fqn: str, column: str) -> int:
    """Возвращает текущее значение счетчика сиквенса или 0, если еще не использовался."""
    seqs = load_sequences(source)
    return seqs.get(f"{entity_fqn}.{column}", 0)


def reset_table_sequences(source: str, entity_fqn: str) -> None:
    """Удаляет сохраненные счетчики сиквенсов для указанной таблицы."""
    seqs = load_sequences(source)
    prefix = f"{entity_fqn}."
    keys_to_remove = [k for k in seqs if k.startswith(prefix)]
    if keys_to_remove:
        for k in keys_to_remove:
            del seqs[k]
        save_sequences(source, seqs)


def rename_table_sequences(source: str, old_fqn: str, new_fqn: str) -> None:
    """Переименовывает префиксы сущностей в словаре счетчиков сиквенсов при rename_table."""
    seqs = load_sequences(source)
    old_prefix = f"{old_fqn}."
    changed = False
    new_seqs = {}
    for k, v in seqs.items():
        if k.startswith(old_prefix):
            col_part = k[len(old_prefix):]
            new_seqs[f"{new_fqn}.{col_part}"] = v
            changed = True
        else:
            new_seqs[k] = v
    if changed:
        save_sequences(source, new_seqs)


def rename_column_sequence(source: str, entity_fqn: str, old_col: str, new_col: str) -> None:
    """Переименовывает колонку в словаре счетчиков сиквенсов при rename_column."""
    seqs = load_sequences(source)
    old_key = f"{entity_fqn}.{old_col}"
    if old_key in seqs:
        val = seqs.pop(old_key)
        seqs[f"{entity_fqn}.{new_col}"] = val
        save_sequences(source, seqs)


def sync_sequences_after_operations(source: str, operations: list[Any]) -> None:
    """Синхронизирует состояние сиквенсов после успешного выполнения DDL операций."""
    for op in operations:
        op_name = getattr(op, "op", "")
        if op_name == "drop_table":
            entity_fqn = getattr(op, "entity_fqn", None) or f"{getattr(op, 'schema_', '')}.{getattr(op, 'name', '')}"
            reset_table_sequences(source, entity_fqn)
        elif op_name == "rename_table":
            schema_ = getattr(op, "schema_", "")
            from_name = getattr(op, "from_name", "") or getattr(op, "table", "")
            to_name = getattr(op, "to_name", "")
            rename_table_sequences(source, f"{schema_}.{from_name}", f"{schema_}.{to_name}")
        elif op_name == "transfer_table":
            from_schema = getattr(op, "from_schema", "") or getattr(op, "schema_", "")
            to_schema = getattr(op, "to_schema", "")
            table = getattr(op, "table", "")
            rename_table_sequences(source, f"{from_schema}.{table}", f"{to_schema}.{table}")
        elif op_name == "rename_column":
            entity_fqn = getattr(op, "entity_fqn", None) or f"{getattr(op, 'schema_', '')}.{getattr(op, 'table', '')}"
            from_name = getattr(op, "from_name", "")
            to_name = getattr(op, "to_name", "")
            rename_column_sequence(source, entity_fqn, from_name, to_name)


def reset_sequences(source: str) -> None:
    """Удаляет персистентный файл сиквенсов и сбрасывает кэш в памяти."""
    global _sequences_cache
    if source in _sequences_cache:
        del _sequences_cache[source]
    sf = _sequences_file(source)
    if sf.exists():
        try:
            sf.unlink()
        except Exception:
            pass



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
    reset_sequences(source)
