"""Статический реестр поддерживаемых реализаций источников.

Жёсткие правила:
- неизвестный тип движка — явный отказ без fallback («не MSSQL -> Oracle» запрещён);
- дубли ключей — ошибка инициализации реестра;
- выбор backend выполняется один раз на границе сценария, а не в каждой функции;
- импорт реестра не открывает Docker, файлы секретов и соединения с СУБД.
"""
from __future__ import annotations

import logging

from .contracts import FAMILY_RDBMS, SourceBackend

logger = logging.getLogger("sydafa.backends.registry")


class UnknownEngineError(ValueError):
    """Неизвестный тип движка источника; перечислены поддерживаемые типы."""

    def __init__(self, engine_type: str):
        supported = ", ".join(sorted(_BACKENDS)) or "(пусто)"
        super().__init__(
            f"Неподдерживаемый тип СУБД '{engine_type}'. Поддерживаемые типы: {supported}"
        )
        self.engine_type = engine_type


class EngineFamilyError(ValueError):
    """Движок зарегистрирован, но не принадлежит требуемому семейству."""

    def __init__(self, engine_type: str, family: str, expected: str):
        super().__init__(
            f"Движок '{engine_type}' принадлежит семейству '{family}', а сценарий требует '{expected}'"
        )
        self.engine_type = engine_type
        self.family = family


_BACKENDS: dict[str, SourceBackend] = {}


def _register(backend: SourceBackend) -> None:
    key = backend.engine_type.strip().lower()
    if key in _BACKENDS:
        raise RuntimeError(f"Дублирующая регистрация backend для типа '{key}'")
    _BACKENDS[key] = backend


def registered_types() -> tuple[str, ...]:
    return tuple(sorted(_BACKENDS))


def has_engine(engine_type: str) -> bool:
    return engine_type.strip().lower() in _BACKENDS


def require(engine_type: str) -> SourceBackend:
    """Возвращает зарегистрированный backend или UnknownEngineError.

    Любой новый SQL для движка обязан строиться через этот выбор; fallback
    на «похожий» диалект запрещён (proposal-1.md §3.2).
    """
    key = engine_type.strip().lower()
    backend = _BACKENDS.get(key)
    if backend is None:
        raise UnknownEngineError(engine_type)
    return backend


def require_rdbms(engine_type: str) -> SourceBackend:
    """Как require(), но дополнительно проверяет семейство RDBMS.

    Вызывать до wake и резервирования состояния: не-RDBMS источник не должен
    дойти до реляционного execution-пути.
    """
    backend = require(engine_type)
    if backend.family != FAMILY_RDBMS:
        raise EngineFamilyError(engine_type, backend.family, FAMILY_RDBMS)
    return backend


def _load_builtin_backends() -> None:
    from .rdbms.mssql import MssqlBackend
    from .rdbms.oracle import OracleBackend

    _register(MssqlBackend())
    _register(OracleBackend())


_load_builtin_backends()
