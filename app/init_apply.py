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
        return {"applied_sources": {}}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"applied_sources": {}}
        if "applied_sources" not in data:
            data["applied_sources"] = {}
        return data
    except Exception:
        return {"applied_sources": {}}


def _save_state_data(data: dict[str, Any]) -> None:
    f = _state_file()
    f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def is_init_applied(source: str | None = None) -> bool:
    """Проверяет флаг фиксации применения init.json (для источника или глобально)."""
    data = _load_state_data()
    applied = data.get("applied_sources", {})
    if source is not None:
        return bool(applied.get(source, {}).get("applied") is True)
    return bool(data.get("applied") is True or bool(applied))


def mark_init_applied(source: str) -> None:
    """Фиксирует факт применения init.json в /state/init_state.json для источника."""
    data = _load_state_data()
    applied = data.setdefault("applied_sources", {})
    applied[source] = {
        "applied": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data["applied"] = True
    _save_state_data(data)
    logger.info("Зафиксирован флаг применения init.json для источника '%s'", source)


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
    """Применяет init.json строго через 2 ручки API: POST /sources и POST /sources/{source}/exec."""
    if is_init_applied(source):
        logger.info("init.json для источника '%s' уже применен ранее (флаг applied=True). Игнорируется.", source)
        return

    init_sources = load_init_data()
    if source not in init_sources:
        logger.info("Источник '%s' отсутствует в init.json, оставляем чистую базу.", source)
        mark_init_applied(source)
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

    mark_init_applied(source)
