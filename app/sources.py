"""Загрузчик реквизитов источников данных из config/sources/{source}.yaml."""
from __future__ import annotations

from pathlib import Path
import yaml

from .constants import CONFIG_SOURCES_DIR
from .models import SourceConfig

_sources: dict[str, SourceConfig] = {}


def load_sources(config_dir: str | Path | None = None) -> dict[str, SourceConfig]:
    global _sources
    p = Path(config_dir or CONFIG_SOURCES_DIR)
    if not p.exists() or not p.is_dir():
        return {}

    loaded: dict[str, SourceConfig] = {}
    for item in p.glob("*.yaml"):
        source_name = item.stem
        raw = yaml.safe_load(item.read_text(encoding="utf-8")) or {}
        loaded[source_name] = SourceConfig(**raw)

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
