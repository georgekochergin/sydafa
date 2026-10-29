"""Контракты расширения источников: семейство, runtime и готовность.

Здесь объявлены только интерфейсы и данные обмена. Реализации живут в
`backends/rdbms/<engine>/`; общий слой (lifecycle, оркестрация) работает
исключительно через эти контракты и `backends/registry`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import ConnectionDetails, SourceConfig
from ..secrets import AppSecrets

FAMILY_RDBMS = "rdbms"


@dataclass(frozen=True)
class ContainerSpec:
    """Данные запуска контейнера СУБД: образ, env, путь данных в контейнере.

    Имена контейнеров/томов, внешние port bindings, сеть, Compose-метки и
    операции Docker SDK собирает общий `container_manager` — backend не
    получает DockerClient.
    """

    image: str
    environment: dict[str, str]
    data_path: str


@dataclass(frozen=True)
class ReadyContext:
    """Вход провайдера runtime: конфиг, секреты, признак инициализации и
    фактический внутренний endpoint (имя контейнера как хост подключения).

    endpoint_host вычисляется общим слоем; vendor-код не импортирует
    container_manager и не знает правил именования.
    """

    config: SourceConfig
    secrets: AppSecrets
    is_initialized: bool
    endpoint_host: str


@dataclass(frozen=True)
class ReadyResult:
    """Результат одной попытки readiness: готов/не готов и безопасная причина."""

    ready: bool
    reason: str | None = None


class RuntimeProvider(Protocol):
    """Провайдер среды выполнения конкретного движка.

    Общий код отвечает за deadline/retry (цикл ожидания); backend — за
    одну попытку проверки сервиса, данные запуска и идемпотентный bootstrap.
    """

    def build_container_spec(self, config: SourceConfig, secrets: AppSecrets) -> ContainerSpec:
        """Образ, env и путь данных тома для контейнера движка."""

    def probe_ready(self, ctx: ReadyContext) -> ReadyResult:
        """Одна попытка проверки готовности СУБД (TCP-порт её не заменяет)."""

    def provision(self, ctx: ReadyContext) -> None:
        """Идемпотентный bootstrap: рабочая БД, DBA, публичный reader."""

    @property
    def startup_timeout(self) -> int:
        """Таймаут ожидания готовности движка в секундах."""


class SourceBackend(Protocol):
    """Реализация источника: канонический ключ, семейство, runtime и описание подключения.

    `family` — внутренняя классификация (сейчас только `rdbms`); в публичный
    API значение не сериализуется.
    """

    engine_type: str
    family: str
    internal_port: int
    runtime: RuntimeProvider

    def describe_connection(self, host: str, port: int) -> ConnectionDetails:
        """Чистое построение публичного ConnectionDetails по переданному endpoint."""
