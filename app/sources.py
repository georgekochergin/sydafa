"""Реестр источников данных СУБД (из init.json или динамически через Orchestration API)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .constants import DEFAULT_INTERNAL_PORTS, READER_USER
from .models import SourceConfig

logger = logging.getLogger("sydafa.sources")
_sources: dict[str, SourceConfig] = {}


def _find_init_file() -> Path | None:
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


def _load_sources_from_init() -> dict[str, SourceConfig]:
    init_path = _find_init_file()
    if not init_path:
        return {}
    try:
        data = json.loads(init_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Ошибка чтения init.json %s: %s", init_path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    src_dict = data.get("sources", data) if isinstance(data, dict) else {}
    if not isinstance(src_dict, dict):
        return {}

    loaded: dict[str, SourceConfig] = {}
    used_ext_ports: dict[int, str] = {}

    for instance_name, val in src_dict.items():
        if isinstance(val, dict):
            src_type = val.get("type", "").lower()
        else:
            src_type = instance_name.lower()

        if src_type not in DEFAULT_INTERNAL_PORTS:
            logger.warning("Пропущен источник '%s': неизвестный тип СУБД '%s'", instance_name, src_type)
            continue

        ext_port_env = os.environ.get(f"SYDAFA_EXT_PORT_{instance_name.upper()}")
        if not ext_port_env and instance_name == src_type:
            ext_port_env = os.environ.get(f"SYDAFA_EXT_PORT_{src_type.upper()}")

        ext_port = int(ext_port_env) if ext_port_env and ext_port_env.isdigit() else None
        if ext_port is not None:
            if ext_port in used_ext_ports:
                logger.error(
                    "Коллизия внешнего порта %d: назначен и для '%s', и для '%s'",
                    ext_port,
                    used_ext_ports[ext_port],
                    instance_name,
                )
            else:
                used_ext_ports[ext_port] = instance_name

        int_port = DEFAULT_INTERNAL_PORTS[src_type]
        host = f"sydafa-src-{src_type}-{instance_name}"

        loaded[instance_name] = SourceConfig(
            name=instance_name,
            type=src_type,
            host=host,
            port=int_port,
            external_port=ext_port,
            reader=READER_USER,
        )
    return loaded


def load_sources() -> dict[str, SourceConfig]:
    global _sources
    _sources = _load_sources_from_init()
    return _sources


def register_source(name: str, src_type: str, external_port: int | None = None) -> SourceConfig:
    global _sources
    src_type = src_type.lower()
    if src_type not in DEFAULT_INTERNAL_PORTS:
        raise ValueError(f"Неподдерживаемый тип СУБД '{src_type}'")

    int_port = DEFAULT_INTERNAL_PORTS[src_type]
    host = f"sydafa-src-{src_type}-{name}"

    cfg = SourceConfig(
        name=name,
        type=src_type,
        host=host,
        port=int_port,
        external_port=external_port,
        reader=READER_USER,
    )
    _sources[name] = cfg
    logger.info("Зарегистрирован источник '%s' (%s, host=%s, ext_port=%s)", name, src_type, host, external_port)
    return cfg


def remove_source(name: str) -> bool:
    global _sources
    if name in _sources:
        del _sources[name]
        logger.info("Удален источник '%s' из реестра", name)
        return True
    return False


def get_source(name: str) -> SourceConfig:
    if name not in _sources:
        load_sources()
    if name not in _sources:
        raise KeyError(f"Источник '{name}' не найден в конфигурации")
    return _sources[name]


def source_names() -> list[str]:
    if not _sources:
        load_sources()
    return list(_sources.keys())


def all_sources() -> dict[str, SourceConfig]:
    if not _sources:
        load_sources()
    return _sources
