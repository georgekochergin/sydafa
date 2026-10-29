"""Менеджер контейнеров источников СУБД через Docker Python SDK."""
from __future__ import annotations

import logging
from pathlib import Path
import re
import shutil
import socket
import threading
import time
from typing import Any

import docker
from docker.errors import NotFound

from .backends import registry
from .backends.contracts import ReadyContext
from . import constants
from .constants import DEFAULT_TTL_MINUTES
from .naming import container_name_for, sanitize_user_token, volume_name_for
from .models import ConnectionDetails, SourceConfig
from .secrets import get_secrets
from .sources import get_source
from . import source_status

logger = logging.getLogger("sydafa.container_manager")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.RLock()


def _source_lock(source: str) -> threading.Lock:
    with _locks_guard:
        if source not in _locks:
            _locks[source] = threading.RLock()
        return _locks[source]


def _get_docker_client() -> docker.DockerClient:
    return docker.from_env()


def _get_context(client: docker.DockerClient) -> tuple[str, str]:
    network_name = "bridge"
    project_name = ""
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


def _wait_for_db_ready(config: SourceConfig, timeout: int | None = None) -> None:
    """Цикл готовности: общий deadline/retry, одну попытку проверки делает backend.

    Порядок vendor-fallback (dba_user -> sa/SYS, CDB/PDB READ WRITE) полностью
    принадлежит runtime-провайдеру движка; TCP-порт readiness не заменяет.
    """
    from . import init_apply

    backend = registry.require(config.type)
    if timeout is None:
        timeout = backend.runtime.startup_timeout
    ctx = ReadyContext(
        config=config,
        secrets=get_secrets(),
        is_initialized=init_apply.is_source_initialized(config.name),
        endpoint_host=container_name_for(config),
    )
    deadline = time.time() + timeout
    last_reason: str | None = None
    while time.time() < deadline:
        result = backend.runtime.probe_ready(ctx)
        if result.ready:
            return
        last_reason = result.reason
        time.sleep(2)
    raise TimeoutError(
        f"Таймаут ожидания готовности {config.type} {config.host}:{config.port}: {last_reason}"
    )


def _ensure_volume(client: docker.DockerClient, volume_name: str) -> None:
    try:
        client.volumes.get(volume_name)
    except NotFound:
        client.volumes.create(name=volume_name, labels={"sydafa.managed": "true"})
        logger.info("Создан volume %s", volume_name)


def reset_data_volume(client: docker.DockerClient, volume: Any) -> None:
    """Clear a Compose-mounted data volume, or remove an unmounted volume."""
    try:
        parent = client.containers.get(socket.gethostname())
    except NotFound:
        volume.remove(force=True)
        return
    for mount in parent.attrs.get("Mounts", []):
        destination = mount.get("Destination", "")
        if (mount.get("Type") == "volume" and mount.get("Name") == volume.name
                and destination.startswith("/source-volumes/")):
            root = Path(destination)
            for entry in root.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            return
    volume.remove(force=True)


def build_connection_details(
    source_name: str,
    config: SourceConfig,
    request_host: str | None = None,
) -> ConnectionDetails:
    is_external = bool(config.external_port)
    port = config.external_port if is_external else config.port
    db_host = container_name_for(config)
    host = "localhost" if is_external else (request_host or db_host)

    # Чистый вызов backend: политика external/internal endpoint — общая,
    # формат URL и учётные данные читателя — у движка. Неизвестный тип
    # здесь получает явный отказ, а не универсальный запасной JDBC URL.
    return registry.require(config.type).describe_connection(host, port)


def _run_container(
    client: docker.DockerClient,
    source_name: str,
    config: SourceConfig,
) -> Any:
    backend = registry.require(config.type)
    secrets = get_secrets()
    spec = backend.runtime.build_container_spec(config, secrets)

    network_name, project_name = _get_context(client)
    name = container_name_for(config)
    volume_name = volume_name_for(config)
    _ensure_volume(client, volume_name)

    labels = {
        "sydafa.managed": "true",
        "sydafa.prefix": constants.CONTAINER_PREFIX,
        "sydafa.source": source_name,
    }
    if project_name:
        labels.update({
            "com.docker.compose.project": project_name,
            "com.docker.compose.service": name,
            "com.docker.compose.config-hash": "sydafa-managed-v1",
            "com.docker.compose.oneoff": "False",
            "com.docker.compose.container-number": "1",
        })
    environment = dict(spec.environment)

    ports = None
    if config.external_port:
        ports = {f"{config.port}/tcp": config.external_port}

    logger.info(
        "Создание контейнера %s (образ %s, volume %s, ports=%s)...",
        name,
        spec.image,
        volume_name,
        ports,
    )
    container = client.containers.run(
        image=spec.image,
        name=name,
        hostname=name,
        detach=True,
        environment=environment,
        ports=ports,
        network=network_name,
        labels=labels,
        volumes={volume_name: {"bind": spec.data_path, "mode": "rw"}},
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


def wake_up(
    source_name: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    publish: bool = False,
    request_host: str | None = None,
) -> dict[str, Any]:
    """Поднимает контейнер источника, гарантирует готовность и обновляет TTL."""
    from . import init_apply, provisioning

    config = get_source(source_name)
    with _source_lock(source_name):
        source_status.mark_starting(source_name)
        client = _get_docker_client()
        name = container_name_for(config)
        container = _get_container(client, name)

        if container is not None:
            # Проверяем, изменился ли external_port в sources.json по сравнению с контейнером.
            # Порты надежно читаются из HostConfig.PortBindings даже когда контейнер остановлен (в отличие от NetworkSettings.Ports).
            current_ext_port: int | None = None
            try:
                host_config_ports = container.attrs.get("HostConfig", {}).get("PortBindings", {}) or {}
                network_ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
                ports_dict = host_config_ports or network_ports
                internal_key = f"{config.port}/tcp"
                bindings = ports_dict.get(internal_key)
                if bindings and isinstance(bindings, list) and len(bindings) > 0:
                    host_port_str = bindings[0].get("HostPort")
                    if host_port_str and host_port_str.isdigit():
                        current_ext_port = int(host_port_str)
            except Exception as e:
                logger.warning("Не удалось инспектировать порты контейнера %s: %s", name, e)

            if current_ext_port != config.external_port:
                logger.info(
                    "Обнаружено изменение external_port для '%s' (%s -> %s). Пересоздание контейнера %s с сохранением данных...",
                    source_name,
                    current_ext_port,
                    config.external_port,
                    name,
                )
                try:
                    container.remove(force=True)
                except Exception as e:
                    logger.warning("Ошибка при удалении устаревшего контейнера %s: %s", name, e)
                container = None

        if container is None:
            container = _run_container(client, source_name, config)

        container.reload()
        if container.status != "running":
            logger.info("Запуск контейнера %s...", name)
            container.start()

        timeout = registry.require(config.type).runtime.startup_timeout
        logger.info("Ожидание порта %s:%s...", config.host, config.port)
        _wait_for_port(config.host, config.port, timeout=timeout)
        logger.info("Ожидание готовности СУБД '%s' (%s)...", source_name, config.type)
        _wait_for_db_ready(config, timeout=timeout)

        provisioning.provision_source(source_name)
        init_apply.apply_init_for_source(source_name)

        conn_details = build_connection_details(source_name, config, request_host=request_host)
        status = source_status.mark_running(
            source_name,
            ttl_minutes,
            config.external_port,
            bool(config.external_port),
            connection=conn_details.model_dump(),
        )
        logger.info("Источник %s готов (host=%s, port=%s).", source_name, conn_details.host, conn_details.port)
        return status


def deactivate(source_name: str) -> dict[str, Any]:
    """Останавливает контейнер СУБД для освобождения ресурсов."""
    config = get_source(source_name)
    with _source_lock(source_name):
        client = _get_docker_client()
        name = container_name_for(config)
        container = _get_container(client, name)
        if container is not None and container.status == "running":
            logger.info("Остановка контейнера %s", name)
            try:
                container.stop(timeout=20)
            except Exception as e:
                logger.warning("Ошибка при остановке %s: %s", name, e)
        return source_status.mark_idle(source_name)


def purge_source_container_and_volume(source_name: str) -> None:
    """Полностью удаляет контейнер СУБД и связанный именованный том данных источника."""
    config = get_source(source_name)
    with _source_lock(source_name):
        client = _get_docker_client()
        cname = container_name_for(config)
        vname = volume_name_for(config)

        container = _get_container(client, cname)
        if container is not None:
            logger.info("Принудительное удаление контейнера %s для реинициализации...", cname)
            try:
                container.remove(force=True, v=True)
            except Exception as e:
                logger.warning("Ошибка удаления контейнера %s: %s", cname, e)

        try:
            vol = client.volumes.get(vname)
            logger.info("Удаление тома данных %s для источника '%s'...", vname, source_name)
            reset_data_volume(client, vol)
        except NotFound:
            pass
        except Exception as e:
            logger.warning("Ошибка удаления тома данных %s: %s", vname, e)


def get_runtime_status(source_name: str, request_host: str | None = None) -> dict[str, Any]:
    config = get_source(source_name)
    status = source_status.load_status(source_name)
    client = _get_docker_client()
    container = _get_container(client, container_name_for(config))
    if container is None:
        status["state"] = "idle"
        status["published"] = bool(config.external_port)
        status["host_port"] = config.external_port
        status["connection"] = None
        return status

    container.reload()
    if container.status == "running" and status.get("state") != "starting":
        status["state"] = "running"
        status["published"] = bool(config.external_port)
        status["host_port"] = config.external_port
        conn_details = build_connection_details(source_name, config, request_host=request_host)
        status["connection"] = conn_details.model_dump()
    elif container.status != "running":
        status["state"] = "idle"
        status["connection"] = None
    return status


def sweep_idle_sources() -> None:
    """Фоновый таймер: остановка по TTL и немедленная остановка несанкционированно запущенных контейнеров."""
    from .sources import get_source, source_names

    client = _get_docker_client()
    for name in source_names():
        config = get_source(name)
        status = source_status.load_status(name)
        cname = container_name_for(config)
        container = _get_container(client, cname)

        if container is not None:
            try:
                container.reload()
            except Exception:
                pass

            # Контейнер включен вручную вне API sydafa или без активного TTL
            # Игнорируем контейнеры в промежуточном статусе 'starting' (в процессе поднятия через wake_up)
            current_state = status.get("state")
            if (
                container.status == "running"
                and current_state != "starting"
                and (current_state != "running" or not status.get("activated_until"))
            ):
                logger.warning(
                    "Обнаружен запущенный контейнер '%s' без активной активации через API sydafa. Немедленная остановка.",
                    cname,
                )
                try:
                    deactivate(name)
                except Exception:
                    logger.exception("Не удалось остановить контейнер '%s'", cname)
                continue

        if source_status.is_expired(status):
            logger.info("Idle-таймер: деактивация источника '%s'", name)
            try:
                deactivate(name)
            except Exception:
                logger.exception("Не удалось деактивировать источник '%s'", name)
