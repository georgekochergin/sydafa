"""Менеджер контейнеров источников СУБД через Docker Python SDK."""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any

import docker
from docker.errors import NotFound

from .constants import (
    CONTAINER_PREFIX,
    DATA_VOLUME_PATHS,
    SOURCE_IMAGES,
)
from .models import SourceConfig
from .secrets import get_secrets
from .sources import get_source
from . import source_status

logger = logging.getLogger("sydafa.container_manager")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _source_lock(source: str) -> threading.Lock:
    with _locks_guard:
        if source not in _locks:
            _locks[source] = threading.Lock()
        return _locks[source]


def container_name_for(config: SourceConfig) -> str:
    return f"{CONTAINER_PREFIX}{config.host}"


def volume_name_for(source_name: str) -> str:
    return f"{CONTAINER_PREFIX}{source_name}-data"


def _get_docker_client() -> docker.DockerClient:
    return docker.from_env()


def _get_context(client: docker.DockerClient) -> tuple[str, str]:
    network_name = "bridge"
    project_name = "sydafa"
    try:
        hostname = socket.gethostname()
        self_container = client.containers.get(hostname)
        labels = self_container.labels or {}
        project_name = labels.get("com.docker.compose.project", project_name)
        networks = list(self_container.attrs.get("NetworkSettings", {}).get("Networks", {}).keys())
        if networks:
            network_name = networks[0]
    except Exception as e:
        logger.warning("Не удалось интроспектировать контейнер sydafa: %s. Используются дефолтные параметры.", e)
    return network_name, project_name


def _wait_for_port(host: str, port: int, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except (OSError, socket.timeout):
            time.sleep(1)
    raise TimeoutError(f"Таймаут ожидания порта {host}:{port} ({timeout}s)")


def _wait_for_db_ready(config: SourceConfig, timeout: int = 180) -> None:
    """Готовность: сначала sydafa (после provisioning), иначе bootstrap sa/SYS."""
    from .constants import SYDAFA_DB, SYDAFA_USER
    from .secrets import get_secrets as _secrets

    secrets = _secrets()
    deadline = time.time() + timeout
    last_err: Exception | None = None

    while time.time() < deadline:
        try:
            if config.type == "mssql":
                import pymssql
                try:
                    conn = pymssql.connect(
                        server=config.host,
                        port=config.port,
                        user=SYDAFA_USER,
                        password=secrets.sydafa_password,
                        database=SYDAFA_DB,
                        login_timeout=3,
                    )
                    conn.close()
                    return
                except Exception as e:
                    last_err = e
                    conn = pymssql.connect(
                        server=config.host,
                        port=config.port,
                        user="sa",
                        password=secrets.mssql_sa_password,
                        database="master",
                        login_timeout=3,
                    )
                    conn.close()
                    return
            elif config.type == "oracle":
                import oracledb
                try:
                    dsn = oracledb.makedsn(config.host, config.port, service_name=SYDAFA_DB)
                    conn = oracledb.connect(user=SYDAFA_USER, password=secrets.sydafa_password, dsn=dsn)
                    conn.close()
                    return
                except Exception as e:
                    last_err = e
                    dsn = oracledb.makedsn(config.host, config.port, service_name="FREE")
                    conn = oracledb.connect(
                        user="SYS",
                        password=secrets.oracle_sys_password,
                        dsn=dsn,
                        mode=oracledb.AUTH_MODE_SYSDBA,
                    )
                    conn.close()
                    return
            else:
                raise ValueError(f"Неизвестный тип СУБД '{config.type}'")
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise TimeoutError(f"Таймаут ожидания готовности {config.type} {config.host}:{config.port}: {last_err}")


def _ensure_volume(client: docker.DockerClient, volume_name: str) -> None:
    try:
        client.volumes.get(volume_name)
    except NotFound:
        client.volumes.create(name=volume_name, labels={"sydafa.managed": "true"})
        logger.info("Создан volume %s", volume_name)


def _published_host_port(container, internal_port: int) -> int | None:
    try:
        container.reload()
    except Exception:
        return None
    key = f"{internal_port}/tcp"
    ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
    bindings = ports.get(key) or []
    if bindings and bindings[0].get("HostPort"):
        return int(bindings[0]["HostPort"])
    host_cfg = (container.attrs.get("HostConfig") or {}).get("PortBindings") or {}
    bindings = host_cfg.get(key) or []
    if bindings and bindings[0].get("HostPort"):
        try:
            return int(bindings[0]["HostPort"])
        except (TypeError, ValueError):
            return None
    return None


def _has_host_ports(container) -> bool:
    try:
        container.reload()
        bindings = (container.attrs.get("HostConfig") or {}).get("PortBindings") or {}
        return bool(bindings)
    except Exception:
        return False


def _run_container(
    client: docker.DockerClient,
    source_name: str,
    config: SourceConfig,
    publish: bool,
) -> Any:
    image_name = SOURCE_IMAGES.get(config.type)
    if not image_name:
        raise ValueError(f"Неизвестный тип СУБД '{config.type}' для источника '{source_name}'")

    network_name, project_name = _get_context(client)
    name = container_name_for(config)
    volume_name = volume_name_for(source_name)
    data_path = DATA_VOLUME_PATHS[config.type]
    _ensure_volume(client, volume_name)

    secrets = get_secrets()
    labels = {
        "com.docker.compose.project": project_name,
        "com.docker.compose.service": name,
        "sydafa.managed": "true",
        "sydafa.source": source_name,
    }
    environment: dict[str, str] = {}
    if config.type == "mssql":
        environment = {
            "ACCEPT_EULA": "Y",
            "MSSQL_SA_PASSWORD": secrets.mssql_sa_password,
            "MSSQL_PID": "Developer",
        }
    elif config.type == "oracle":
        environment = {"ORACLE_PASSWORD": secrets.oracle_sys_password}

    ports = None
    if publish:
        ports = {f"{config.port}/tcp": None}

    logger.info(
        "Создание контейнера %s (образ %s, volume %s, publish=%s)...",
        name,
        image_name,
        volume_name,
        publish,
    )
    container = client.containers.run(
        image=image_name,
        name=name,
        hostname=config.host,
        detach=True,
        environment=environment,
        ports=ports,
        network=network_name,
        labels=labels,
        volumes={volume_name: {"bind": data_path, "mode": "rw"}},
    )
    if name != config.host:
        try:
            net = client.networks.get(network_name)
            net.disconnect(container)
            net.connect(container, aliases=[config.host])
        except Exception as e:
            logger.warning("Не удалось назначить DNS-алиас %s контейнеру %s: %s", config.host, name, e)
    return container


def _get_container(client: docker.DockerClient, name: str):
    try:
        return client.containers.get(name)
    except NotFound:
        return None


def _recreate(client: docker.DockerClient, source_name: str, config: SourceConfig, publish: bool):
    name = container_name_for(config)
    existing = _get_container(client, name)
    if existing is not None:
        logger.info("Пересоздание контейнера %s (publish=%s)", name, publish)
        try:
            existing.stop(timeout=20)
        except Exception:
            pass
        existing.remove(v=False)
    return _run_container(client, source_name, config, publish)


def wake_up(source_name: str, ttl_minutes: int, publish: bool = False) -> dict[str, Any]:
    """Поднимает контейнер источника, при необходимости публикует host-порт."""
    from . import init_apply, provisioning

    config = get_source(source_name)
    with _source_lock(source_name):
        source_status.mark_starting(source_name)
        client = _get_docker_client()
        name = container_name_for(config)
        container = _get_container(client, name)

        needs_recreate = False
        if container is None:
            container = _run_container(client, source_name, config, publish)
        else:
            has_ports = _has_host_ports(container)
            if publish and not has_ports:
                needs_recreate = True
            if not publish and has_ports:
                needs_recreate = True
            if needs_recreate:
                container = _recreate(client, source_name, config, publish)

        container.reload()
        if container.status != "running":
            logger.info("Запуск контейнера %s...", name)
            container.start()

        timeout = 180 if config.type == "oracle" else 90
        logger.info("Ожидание порта %s:%s...", config.host, config.port)
        _wait_for_port(config.host, config.port, timeout=timeout)
        logger.info("Ожидание готовности СУБД '%s' (%s)...", source_name, config.type)
        _wait_for_db_ready(config, timeout=timeout)

        provisioning.provision_source(source_name)
        init_apply.apply_init_for_source(source_name)

        host_port = _published_host_port(container, config.port) if publish else None
        status = source_status.mark_running(source_name, ttl_minutes, host_port, publish)
        logger.info("Источник %s готов (publish=%s, host_port=%s).", source_name, publish, host_port)
        return status


def deactivate(source_name: str) -> dict[str, Any]:
    """Останавливает контейнер; снимает host-порты через recreate без публикации."""
    config = get_source(source_name)
    with _source_lock(source_name):
        client = _get_docker_client()
        name = container_name_for(config)
        container = _get_container(client, name)
        if container is not None:
            if _has_host_ports(container):
                logger.info("Снятие host-портов и остановка %s", name)
                try:
                    container.stop(timeout=20)
                except Exception:
                    pass
                container.remove(v=False)
                created = _run_container(client, source_name, config, publish=False)
                try:
                    created.stop(timeout=20)
                except Exception:
                    pass
            elif container.status == "running":
                logger.info("Остановка контейнера %s", name)
                container.stop(timeout=20)
        return source_status.mark_idle(source_name)


def get_runtime_status(source_name: str) -> dict[str, Any]:
    config = get_source(source_name)
    status = source_status.load_status(source_name)
    client = _get_docker_client()
    container = _get_container(client, container_name_for(config))
    if container is None:
        status["state"] = "idle"
        status["published"] = False
        status["host_port"] = None
        return status
    container.reload()
    if container.status == "running" and status.get("state") != "starting":
        status["state"] = "running"
        if _has_host_ports(container):
            status["published"] = True
            status["host_port"] = _published_host_port(container, config.port)
        else:
            status["published"] = False
            status["host_port"] = None
    elif container.status != "running":
        status["state"] = "idle"
        status["published"] = False
        status["host_port"] = None
    return status


def sweep_idle_sources() -> None:
    from .sources import source_names

    for name in source_names():
        status = source_status.load_status(name)
        if source_status.is_expired(status):
            logger.info("Idle-таймер: деактивация источника '%s'", name)
            try:
                deactivate(name)
            except Exception:
                logger.exception("Не удалось деактивировать источник '%s'", name)
