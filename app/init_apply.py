"""Управление инициализацией init.json, маркером состояния и самоочисткой (Self-Healing)."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from .backends.rdbms.initialization import compile_schema_decl_to_operations
from .constants import DEFAULT_TTL_MINUTES, STATE_DIR
from .models import ExecOperation, ExecRequest
from .sources import find_sources_file, get_source

logger = logging.getLogger("sydafa.init_apply")

_INIT_STATE_FILE = "init_state.json"


def _state_file() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / _INIT_STATE_FILE


def _load_state_data() -> dict[str, Any]:
    f = _state_file()
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _save_state_data(data: dict[str, Any]) -> None:
    f = _state_file()
    f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def is_source_initialized(source: str) -> bool:
    """Проверяет, была ли уже выполнена первичная инициализация для конкретного источника."""
    data = _load_state_data()
    applied_sources = data.get("applied_sources", {})
    if isinstance(applied_sources, dict):
        return source in applied_sources
    return False


def get_source_init_timestamp(source: str) -> str | None:
    """Возвращает метку времени завершения инициализации конкретного источника."""
    data = _load_state_data()
    applied_sources = data.get("applied_sources", {})
    if isinstance(applied_sources, dict) and source in applied_sources:
        return str(applied_sources[source])
    return None


def get_init_timestamp() -> str | None:
    """Возвращает общую метку последней инициализации для /health."""
    data = _load_state_data()
    ts = data.get("applied_at")
    if ts:
        return str(ts)
    applied_sources = data.get("applied_sources", {})
    if isinstance(applied_sources, dict) and applied_sources:
        return max(str(v) for v in applied_sources.values())
    return None


def mark_source_initialized(source: str) -> None:
    """Фиксирует факт применения первичной инициализации для источника."""
    data = _load_state_data()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    applied_sources = data.setdefault("applied_sources", {})
    if not isinstance(applied_sources, dict):
        applied_sources = {}
        data["applied_sources"] = applied_sources
    applied_sources[source] = now_iso
    data["applied"] = True
    data["applied_at"] = now_iso
    _save_state_data(data)
    logger.info("Зафиксирована первичная инициализация для источника '%s' (applied_at=%s)", source, now_iso)


def reset_source_init(source: str) -> None:
    """Сбрасывает факт инициализации и файлы состояния для конкретного источника."""
    data = _load_state_data()
    applied_sources = data.get("applied_sources", {})
    if isinstance(applied_sources, dict) and source in applied_sources:
        del applied_sources[source]
        _save_state_data(data)
        logger.info("Сброшена запись инициализации для '%s' в init_state.json", source)

    # Удаляем сохраненную live-схему
    schema_file = STATE_DIR / f"live_schema.{source}.json"
    if schema_file.exists():
        try:
            schema_file.unlink()
            logger.info("Удален файл live_schema для '%s'", source)
        except Exception as e:
            logger.warning("Не удалось удалить %s: %s", schema_file, e)

    # Удаляем сохраненный статус источника
    status_file = STATE_DIR / f"source_status.{source}.json"
    if status_file.exists():
        try:
            status_file.unlink()
            logger.info("Удален файл source_status для '%s'", source)
        except Exception as e:
            logger.warning("Не удалось удалить %s: %s", status_file, e)

    # Удаляем сохраненный счетчик батчей и сиквенсов
    from .schema_state import reset_batch_sequence, reset_sequences
    reset_batch_sequence(source)
    reset_sequences(source)


def load_manifest_sources() -> dict[str, Any]:
    """Считывает источники из манифеста sources.json."""
    sources_path = find_sources_file()
    if not sources_path:
        return {}
    try:
        content = json.loads(sources_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Ошибка чтения манифеста источников %s: %s", sources_path, e)
        return {}
    if not isinstance(content, dict):
        return {}
    return content.get("sources", content) if isinstance(content, dict) else {}


def check_and_heal_orphaned_state() -> None:
    """Если /state пуст, а контейнеры баз или тома живы — снести их для пересоздания с нуля."""
    from .container_manager import _get_docker_client

    state_empty = not _state_file().exists() and not (STATE_DIR / "secrets.json").exists()
    if not state_empty:
        return

    try:
        client = _get_docker_client()
    except Exception as e:
        logger.warning("Не удалось подключиться к Docker для проверки self-healing: %s", e)
        return

    from .container_manager import _get_docker_client, sanitize_user_token

    prefix_raw = os.environ.get("SYDAFA_CONTAINER_PREFIX", "")
    prefix = sanitize_user_token(prefix_raw)
    target_c_prefix = f"sydafa-{prefix}-src-" if prefix else "sydafa-src-"
    target_v_prefix = f"sydafa-{prefix}-data-" if prefix else "sydafa-data-"

    healed_containers = 0
    try:
        for c in client.containers.list(all=True):
            c_name = c.name or ""
            labels = c.labels or {}
            # Удаляем контейнеры текущего пространства имен (или старые legacy контейнеры, если префикс пуст)
            is_match = c_name.startswith(target_c_prefix)
            if not prefix:
                is_match = is_match or ("sydafa" in c_name and "-source" in c_name)
            if is_match or (labels.get("sydafa.managed") == "true" and (not prefix or labels.get("sydafa.prefix") == prefix)):
                logger.warning("Self-Healing: удаление осиротевшего контейнера %s (пустой /state)", c_name)
                try:
                    c.remove(force=True, v=True)
                    healed_containers += 1
                except Exception as ex:
                    logger.warning("Не удалось удалить контейнер %s: %s", c_name, ex)
    except Exception as e:
        logger.warning("Ошибка проверки контейнеров при self-healing: %s", e)

    healed_volumes = 0
    try:
        for v in client.volumes.list():
            v_name = v.name or ""
            labels = v.attrs.get("Labels") or {}
            # Удаляем тома текущего пространства имен (или старые legacy тома, если префикс пуст)
            is_match = v_name.startswith(target_v_prefix)
            if not prefix:
                is_match = is_match or ("sydafa" in v_name and "-data" in v_name)
            if is_match or (labels.get("sydafa.managed") == "true" and (not prefix or labels.get("sydafa.prefix") == prefix)):
                logger.warning("Self-Healing: удаление осиротевшего тома %s (пустой /state)", v_name)
                try:
                    from .container_manager import reset_data_volume

                    reset_data_volume(client, v)
                    healed_volumes += 1
                except Exception as ex:
                    logger.warning("Не удалось удалить volume %s: %s", v_name, ex)
    except Exception as e:
        logger.warning("Ошибка проверки томов при self-healing: %s", e)

    if healed_containers or healed_volumes:
        logger.info(
            "Self-Healing завершен: удалено контейнеров: %d, томов: %d. Состояние очищено для развертывания.",
            healed_containers,
            healed_volumes,
        )


def apply_init_for_source(source: str) -> None:
    """Применяет DDL схемы и правила из sources.json для указанного источника.
    
    Если источник уже был инициализирован ранее, повторный накат пропускается.
    Создаются чистые таблицы и регистрируются правила генерации.
    Накат схемы — операция RDBMS-семейства: семейство проверяется до исполнения.
    """
    if is_source_initialized(source):
        logger.info("Источник '%s' уже инициализирован ранее. Повторная инициализация операций игнорируется.", source)
        return

    from .backends import registry
    registry.require_rdbms(get_source(source).type)

    init_sources = load_manifest_sources()
    if source not in init_sources:
        logger.info("Источник '%s' отсутствует в sources.json, оставляем чистую базу.", source)
        mark_source_initialized(source)
        return

    val = init_sources[source]
    if not isinstance(val, dict):
        val = {}

    from .backends.rdbms import execution as rdbms_execution

    # 1. Загрузка правил генерации rules (если заданы)
    rules_raw = val.get("rules", {})
    if isinstance(rules_raw, dict) and rules_raw:
        from .models import TableRuleProfile
        from .rules_state import set_table_rules
        logger.info("Манифест sources.json: сохранение %d профилей правил для '%s'...", len(rules_raw), source)
        for entity_fqn, prof_data in rules_raw.items():
            try:
                prof = TableRuleProfile(**prof_data) if isinstance(prof_data, dict) else prof_data
                set_table_rules(source, entity_fqn, prof)
            except Exception as e:
                logger.warning("Не удалось сохранить профиль правил для '%s.%s': %s", source, entity_fqn, e)

    # 2. Декларативная схема schema (приоритетный формат)
    schema_raw = val.get("schema", {})
    if isinstance(schema_raw, dict) and schema_raw.get("tables"):
        ddl_ops = compile_schema_decl_to_operations(schema_raw, rules_data=rules_raw)
        logger.info("Манифест sources.json: выполнение %d DDL операций декларативной схемы для '%s'...", len(ddl_ops), source)
        rdbms_execution.execute_source_operations(source, ddl_ops)

    # 3. Поддержка legacy operations (только DDL, исключая автозаливку append)
    elif "operations" in val:
        raw_ops = val.get("operations", [])
        if raw_ops:
            adapter = TypeAdapter(list[ExecOperation])
            parsed_ops = adapter.validate_python(raw_ops)
            # Исключаем append операции для соблюдения принципа чистой схемы
            filtered_ops = [op for op in parsed_ops if getattr(op, "op", "") != "append"]
            if filtered_ops:
                logger.info("Манифест sources.json: выполнение %d legacy DDL операций для '%s'...", len(filtered_ops), source)
                rdbms_execution.execute_source_operations(source, filtered_ops)
            else:
                logger.info("Манифест sources.json: для источника '%s' нет DDL операций.", source)

    else:
        logger.info("Манифест sources.json: для источника '%s' нет декларативной схемы (чистая база).", source)

    mark_source_initialized(source)


def seed_all_sources_on_first_startup() -> None:
    """Выполняет eager-инициализацию новых источников из sources.json.
    
    Для каждого неинициализированного источника:
    1. Поднимается СУБД (wake_up), выполняется bootstrap и накатываются DDL+DML из манифеста.
    2. Контейнер СУБД сразу же останавливается (deactivate) для освобождения памяти.
    3. Фиксируется факт применения в applied_sources в /state/init_state.json.
    
    Источники, которые уже были инициализированы ранее, пропускаются (0 MB RAM).
    """
    init_sources = load_manifest_sources()
    if not init_sources:
        logger.info("Манифест sources.json пуст или не содержит источников.")
        return

    sources_to_init = [s for s in init_sources.keys() if not is_source_initialized(s)]
    if not sources_to_init:
        logger.info(
            "Все источники из sources.json уже были инициализированы ранее. Пропуск первичной инициализации (RAM=0 MB)."
        )
        return

    logger.info(
        "Обнаружено %d неинициализированных источников для развертывания: %s",
        len(sources_to_init),
        sources_to_init,
    )

    from . import container_manager

    for src_name in sources_to_init:
        logger.info(">>> Первичная инициализация источника '%s': запуск СУБД и развертывание схемы/правил...", src_name)
        try:
            container_manager.wake_up(src_name, ttl_minutes=DEFAULT_TTL_MINUTES)
        except Exception as e:
            logger.critical("Сбой при первичной инициализации источника '%s': %s", src_name, e, exc_info=True)
            raise

        logger.info("Развертывание источника '%s' завершено. Немедленная остановка контейнера для освобождения RAM...", src_name)
        try:
            container_manager.deactivate(src_name)
        except Exception as e:
            logger.warning("Не удалось остановить контейнер источника '%s' после инициализации: %s", src_name, e)

        logger.info("Контейнер источника '%s' успешно остановлен.", src_name)
        mark_source_initialized(src_name)

    logger.info(
        "Инициализация завершена. Все СУБД остановлены."
    )


def reinit_source(source_name: str) -> dict[str, Any]:
    """Выполняет полный точечный сброс и повторную инициализацию для указанного источника.
    
    1. Останавливает и удаляет контейнер СУБД источника и его именованный том данных.
    2. Очищает live_schema, source_status и запись в applied_sources.
    3. Перечитывает манифест sources.json и выполняет bootstrap + накат свежих operations.
    4. Немедленно останавливает СУБД источника для освобождения оперативной памяти.
    """
    from . import container_manager, schema_state, sources

    logger.warning(">>> Начало процедуры РЕИНИЦИАЛИЗАЦИИ источника '%s'...", source_name)

    # 1. Зачистка контейнера и тома данных
    container_manager.purge_source_container_and_volume(source_name)

    # 2. Сброс метаданных состояния в /state и памяти
    reset_source_init(source_name)
    schema_state.reset_memory_cache(source_name)

    # 3. Перечитывание реестра источников на случай обновления sources.json
    sources.load_sources()

    # 4. Выполнение первичной инициализации
    manifest = load_manifest_sources()
    src_spec = manifest.get(source_name, {})
    raw_ops = src_spec.get("operations", []) if isinstance(src_spec, dict) else []

    logger.info("Реинициализация '%s': запуск чистой базы и применение %d операций...", source_name, len(raw_ops))
    container_manager.wake_up(source_name, ttl_minutes=DEFAULT_TTL_MINUTES)

    logger.info("Реинициализация '%s': глушение контейнера для возврата 0 MB RAM...", source_name)
    container_manager.deactivate(source_name)
    mark_source_initialized(source_name)

    ts = get_source_init_timestamp(source_name) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "status": "reinitialized",
        "source": source_name,
        "applied_at": ts,
        "operations_executed": len(raw_ops),
        "message": f"Источник '{source_name}' успешно зачищен и реинициализирован с {len(raw_ops)} операциями.",
    }
