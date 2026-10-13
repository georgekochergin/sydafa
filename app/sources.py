"""Загрузчик конфигурации источников данных (из init.json или config/sources/{source}.yaml)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import yaml

from .constants import CONFIG_SOURCES_DIR, DEFAULT_INTERNAL_PORTS, READER_USER
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
    for name in src_dict.keys():
        src_type = name.lower()
        if src_type not in DEFAULT_INTERNAL_PORTS:
            continue
        ext_port_env = os.environ.get(f"SYDAFA_EXT_PORT_{name.upper()}")
        ext_port = int(ext_port_env) if ext_port_env and ext_port_env.isdigit() else None
        int_port = DEFAULT_INTERNAL_PORTS[src_type]
        loaded[name] = SourceConfig(
            type=src_type,
            host=f"sydafa-{name}-source",
            port=int_port,
            external_port=ext_port,
            reader=READER_USER,
        )
    return loaded


def load_sources(config_dir: str | Path | None = None) -> dict[str, SourceConfig]:
    global _sources
    loaded: dict[str, SourceConfig] = {}

    # 1. Из init.json
    from_init = _load_sources_from_init()
    loaded.update(from_init)

    # 2. Из *.yaml (для совместимости или переопределения)
    p = Path(config_dir or CONFIG_SOURCES_DIR)
    if p.exists() and p.is_dir():
        for item in p.glob("*.yaml"):
            source_name = item.stem
            raw = yaml.safe_load(item.read_text(encoding="utf-8")) or {}
            src_type = raw.get("type", source_name)
            int_port = raw.get("port") or DEFAULT_INTERNAL_PORTS.get(src_type, 1433)
            ext_port_env = os.environ.get(f"SYDAFA_EXT_PORT_{source_name.upper()}")
            ext_port = int(ext_port_env) if ext_port_env and ext_port_env.isdigit() else raw.get("external_port")
            host = raw.get("host") or f"sydafa-{source_name}-source"
            reader = raw.get("reader") or READER_USER
            loaded[source_name] = SourceConfig(
                type=src_type,
                host=host,
                port=int_port,
                external_port=ext_port,
                reader=reader,
            )

    _sources = loaded
    return _sources


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
