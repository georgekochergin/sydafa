"""Менеджер контейнеров источников СУБД через Docker Python SDK."""
from __future__ import annotations

import io
import logging
import os
import socket
import tarfile
import time
from typing import Any

import docker
from docker.errors import NotFound

from .constants import SOURCE_IMAGES, TEMPLATES_DIR
from .models import SourceConfig
from .sources import all_sources

logger = logging.getLogger("sydafa.container_manager")


def _get_docker_client() -> docker.DockerClient:
    return docker.from_env()


def _get_context(client: docker.DockerClient) -> tuple[str, str]:
    """Определяет сеть Compose и имя проекта по метаданным контейнера sydafa."""
    network_name = "dlh-poc"
    project_name = "datalake_stand"

    try:
        hostname = socket.gethostname()
        self_container = client.containers.get(hostname)
        # Имя проекта из лейблов
        labels = self_container.labels or {}
        project_name = labels.get("com.docker.compose.project", project_name)

        # Сеть
        networks = list(self_container.attrs.get("NetworkSettings", {}).get("Networks", {}).keys())
        if networks:
            network_name = networks[0]
    except Exception as e:
        logger.warning("Не удалось интроспектировать контейнер sydafa: %s. Используются дефолтные параметры.", e)

    return network_name, project_name


def _wait_for_port(host: str, port: int, timeout: int = 120) -> None:
    """Ожидает открытия TCP-порта."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except (OSError, socket.timeout):
            time.sleep(1)
    raise TimeoutError(f"Таймаут ожидания порта {host}:{port} ({timeout}s)")


def ensure_source_container(source_name: str, config: SourceConfig) -> None:
    """Гарантирует, что контейнер источника создан, запущен и доступен по сети."""
    client = _get_docker_client()
    network_name, project_name = _get_context(client)

    image_name = SOURCE_IMAGES.get(config.type)
    if not image_name:
        raise ValueError(f"Неизвестный тип СУБД '{config.type}' для источника '{source_name}'")

    container_name = config.host

    # Проверка существующего контейнера
    try:
        container = client.containers.get(container_name)
        if container.status != "running":
            logger.info("Запуск существующего контейнера %s...", container_name)
            container.start()
        else:
            logger.info("Контейнер %s уже запущен.", container_name)
    except NotFound:
        logger.info("Создание контейнера %s (образ %s, сеть %s)...", container_name, image_name, network_name)

        # Базовые параметры контейнера
        environment: dict[str, str] = {}
        ports: dict[str, int] = {f"{config.port}/tcp": config.port}
        entrypoint: list[str] | str | None = None
        command: list[str] | str | None = None

        labels = {
            "com.docker.compose.project": project_name,
            "com.docker.compose.service": container_name,
            "sydafa.managed": "true",
        }

        if config.type == "mssql":
            sa_password = os.environ.get("MSSQL_SA_PASSWORD", "AdminPassword1!")
            environment = {
                "ACCEPT_EULA": "Y",
                "MSSQL_SA_PASSWORD": sa_password,
                "MSSQL_PID": "Developer",
            }
            # Автономный entrypoint без внешних монтирований
            entrypoint = [
                "/bin/bash",
                "-c",
                (
                    "/opt/mssql/bin/sqlservr &\n"
                    "pid=$!\n"
                    "until /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P \"$MSSQL_SA_PASSWORD\" -Q 'SELECT 1' >/dev/null 2>&1; do sleep 1; done\n"
                    "/opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P \"$MSSQL_SA_PASSWORD\" -Q \"\n"
                    "IF NOT EXISTS (SELECT * FROM sys.server_principals WHERE name = 'sydafa')\n"
                    "BEGIN\n"
                    "    CREATE LOGIN sydafa WITH PASSWORD = 'sydafa', CHECK_POLICY = OFF;\n"
                    "    ALTER SERVER ROLE sysadmin ADD MEMBER sydafa;\n"
                    "END\"\n"
                    "wait $pid\n"
                ),
            ]

            client.containers.run(
                image=image_name,
                name=container_name,
                hostname=container_name,
                detach=True,
                environment=environment,
                ports=ports,
                entrypoint=entrypoint,
                command=command,
                network=network_name,
                labels=labels,
            )

        elif config.type == "oracle":
            environment = {
                "ORACLE_PASSWORD": "admin",
            }
            # Создаем контейнер Oracle
            created_container = client.containers.create(
                image=image_name,
                name=container_name,
                hostname=container_name,
                environment=environment,
                ports=ports,
                network=network_name,
                labels=labels,
            )

            # Передаем встроенный скрипт инициализации sydafa/sydafa в каталог /container-entrypoint-initdb.d/
            sql_script_path = TEMPLATES_DIR / "init" / "oracle" / "01-init-sydafa.sql"
            if sql_script_path.exists():
                logger.info("Инъекция скрипта инициализации %s в контейнер %s...", sql_script_path.name, container_name)
                tar_stream = io.BytesIO()
                with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                    sql_data = sql_script_path.read_bytes()
                    ti = tarfile.TarInfo(name="01-init-sydafa.sql")
                    ti.size = len(sql_data)
                    ti.mtime = int(time.time())
                    ti.mode = 0o644
                    tar.addfile(ti, io.BytesIO(sql_data))
                tar_stream.seek(0)
                created_container.put_archive("/container-entrypoint-initdb.d", tar_stream.getvalue())

            # Запускаем созданный контейнер
            created_container.start()

    # Ожидание доступности TCP порта внутри сети
    timeout = 180 if config.type == "oracle" else 90
    logger.info("Ожидание доступности порта %s:%s...", config.host, config.port)
    _wait_for_port(config.host, config.port, timeout=timeout)
    logger.info("Источник %s (%s:%s) готов к приёму соединений.", source_name, config.host, config.port)


def ensure_all_source_containers() -> None:
    """Запускает и проверяет все контейнеры источников, определённые в конфигурации."""
    sources = all_sources()
    for name, conf in sources.items():
        ensure_source_container(name, conf)
