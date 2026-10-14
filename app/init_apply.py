"""Управление инициализацией init.json, маркером состояния и самоочисткой (Self-Healing)."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from .constants import STATE_DIR
from .models import CreateSourceRequest, ExecOperation, ExecRequest

logger = logging.getLogger("sydafa.init_apply")

_INIT_STATE_FILE = "init_state.json"


def _state_file() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / _INIT_STATE_FILE


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


def is_init_applied() -> bool:
    """Проверяет глобальный флаг фиксации применения init.json.
    
    Любые изменения init.json после первой успешной инициализации игнорируются.
    """
    data = _load_state_data()
    return bool(data.get("applied") is True)


def get_init_timestamp() -> str | None:
    """Возвращает метку времени завершения первичной инициализации в формате ISO 8601 UTC."""
    data = _load_state_data()
    ts = data.get("applied_at")
    return str(ts) if ts else None


def mark_init_applied() -> None:
    """Фиксирует глобальный факт применения init.json в /state/init_state.json."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {
        "applied": True,
        "applied_at": now_iso,
    }
    _save_state_data(data)
    logger.info("Зафиксирован флаг первичной инициализации init.json (applied=True, applied_at=%s)", now_iso)


def load_init_data() -> dict[str, Any]:
    """Считывает манифест init.json."""
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

    healed_containers = 0
    try:
        for c in client.containers.list(all=True):
            c_name = c.name or ""
            labels = c.labels or {}
            if ("sydafa" in c_name and "-source" in c_name) or labels.get("sydafa.managed") == "true":
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
            if ("sydafa" in v_name and "-data" in v_name) or labels.get("sydafa.managed") == "true":
                logger.warning("Self-Healing: удаление осиротевшего тома %s (пустой /state)", v_name)
                try:
                    v.remove(force=True)
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
    """Применяет DDL и DML операции из init.json для указанного источника.
    
    Если глобальная инициализация уже зафиксирована (applied=True), повторные операции пропускаются.
    """
    if is_init_applied():
        logger.info("init.json уже применен ранее (флаг applied=True). Повторный сид для '%s' игнорируется.", source)
        return

    init_sources = load_init_data()
    if source not in init_sources:
        logger.info("Источник '%s' отсутствует в init.json, оставляем чистую базу.", source)
        return

    val = init_sources[source]
    if isinstance(val, dict):
        src_type = val.get("type", source).lower()
        raw_ops = val.get("operations", [])
    else:
        src_type = source.lower()
        raw_ops = val if isinstance(val, list) else []

    from . import main, sources

    # Шаг 1: Создание источника через логику POST /sources
    if source not in sources.source_names():
        req = CreateSourceRequest(name=source, type=src_type)
        main.create_source(req, request=None)

    # Шаг 2: Наполнение источника через логику POST /sources/{source}/exec
    if raw_ops:
        logger.info("API Bootstrap: шаг 2 — выполнение %d операций exec для '%s'...", len(raw_ops), source)
        from . import exec_engine
        adapter = TypeAdapter(list[ExecOperation])
        ops = adapter.validate_python(raw_ops)
        exec_engine.execute_source_operations(source, ops)
    else:
        logger.info("API Bootstrap: для источника '%s' нет операций в init.json (чистая база).", source)

    mark_init_applied()


def seed_all_sources_on_first_startup() -> None:
    """Выполняет eager-инициализацию всех источников из init.json при первом старте.
    
    Для каждого источника:
    1. Поднимается контейнер СУБД (wake_up), выполняется bootstrap и накатываются DDL+DML.
    2. Контейнер СУБД сразу же останавливается (deactivate) для освобождения памяти.
    3. Фиксируется глобальный флаг applied: true и applied_at в /state/init_state.json.
    
    При любых последующих перезапусках сервиса или изменениях init.json процедура
    полностью пропускается (инициализация уже произошла).
    """
    if is_init_applied():
        logger.info(
            "Первичная инициализация init.json уже была выполнена ранее (applied=True, applied_at=%s). Пропуск сидинга.",
            get_init_timestamp(),
        )
        return

    init_sources = load_init_data()
    if not init_sources:
        logger.info("Манифест init.json пуст или не содержит источников. Фиксация пустого сидинга.")
        mark_init_applied()
        return

    logger.info(
        "Первичный старт sydafa: обнаружено %d источников в init.json для инициализации: %s",
        len(init_sources),
        list(init_sources.keys()),
    )

    from . import container_manager

    for src_name in init_sources.keys():
        logger.info(">>> Инициализация источника '%s': запуск СУБД и применение схемы/данных...", src_name)
        try:
            container_manager.wake_up(src_name)
        except Exception as e:
            logger.critical("Сбой при первичной инициализации источника '%s': %s", src_name, e, exc_info=True)
            raise

        logger.info("Наполнение источника '%s' завершено. Немедленная остановка контейнера для освобождения RAM...", src_name)
        try:
            container_manager.deactivate(src_name)
        except Exception as e:
            logger.warning("Не удалось остановить контейнер источника '%s' после сидинга: %s", src_name, e)

        logger.info("Контейнер источника '%s' успешно остановлен.", src_name)

    mark_init_applied()
    logger.info(
        "Первичная инициализация всех источников завершена успешно (applied_at=%s). Все СУБД остановлены.",
        get_init_timestamp(),
    )
