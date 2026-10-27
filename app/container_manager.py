"""Менеджер контейнеров источников СУБД через Docker Python SDK."""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from typing import Any

import docker
from docker.errors import NotFound

from .constants import (
    CONTAINER_PREFIX,
    DATA_VOLUME_PATHS,
    DEFAULT_INTERNAL_PORTS,
    DEFAULT_TTL_MINUTES,
    READER_PASSWORD,
    READER_USER,
    SOURCE_IMAGES,
    SYDAFA_DB,
)
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


def sanitize_user_token(token: str | None) -> str:
    """Очищает пользовательский токен (префикс или имя инстанса) от пробелов, краевых дефисов и спецсимволов."""
    if not token:
        return ""
    # Заменяем недопустимые спецсимволы и пробелы на дефис
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(token).strip())
    # Убираем дефисы, точки и подчеркивания по краям и приводим к нижнему регистру
    return cleaned.strip("-._").lower()


def container_name_for(config: SourceConfig) -> str:
    prefix = sanitize_user_token(CONTAINER_PREFIX)
    src_name = sanitize_user_token(config.name)
    parts: list[str] = []
    if prefix.startswith("sydafa"):
        parts.append(prefix)
    else:
        parts.append("sydafa")
        if prefix:
            parts.append(prefix)
    parts.extend(["src", config.type, src_name])
    return "-".join(parts)


def volume_name_for(config: SourceConfig) -> str:
    prefix = sanitize_user_token(CONTAINER_PREFIX)
    src_name = sanitize_user_token(config.name)
    parts: list[str] = []
    if prefix.startswith("sydafa"):
        parts.append(prefix)
    else:
        parts.append("sydafa")
        if prefix:
            parts.append(prefix)
    parts.extend(["data", config.type, src_name])
    return "-".join(parts)


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
    """Готовность: сначала dba_user (после provisioning), иначе bootstrap sa/SYS."""
    from . import init_apply
    secrets = get_secrets()
    deadline = time.time() + timeout
    last_err: Exception | None = None
    is_init = init_apply.is_source_initialized(config.name)

    db_host = container_name_for(config)
    while time.time() < deadline:
        try:
            if config.type == "mssql":
                import pymssql
                try:
                    conn = pymssql.connect(
                        server=db_host,
                        port=config.port,
                        user=secrets.dba_user,
                        password=secrets.dba_password,
                        database=SYDAFA_DB,
                        login_timeout=3,
                    )
                    conn.close()
                    return
                except Exception as e:
                    last_err = e
                    try:
                        conn = pymssql.connect(
                            server=db_host,
                            port=config.port,
                            user="sa",
                            password=secrets.mssql_sa_password,
                            database="master",
                            login_timeout=3,
                        )
                        conn.close()
                        return
                    except Exception as sa_err:
                        last_err = sa_err
            elif config.type == "oracle":
                import oracledb
                try:
                    dsn = oracledb.makedsn(db_host, config.port, service_name=SYDAFA_DB)
                    conn = oracledb.connect(user=secrets.dba_user, password=secrets.dba_password, dsn=dsn)
                    conn.close()
                    return
                except Exception as e:
                    last_err = e
                    try:
                        dsn = oracledb.makedsn(db_host, config.port, service_name="FREE")
                        conn = oracledb.connect(
                            user="SYS",
                            password=secrets.oracle_sys_password,
                            dsn=dsn,
                            mode=oracledb.AUTH_MODE_SYSDBA,
                        )
                        cur = conn.cursor()
                        cur.execute("SELECT open_mode FROM v$database")
                        row = cur.fetchone()
                        # Корень CDB открывается раньше PDB sydafa: готовность
                        # только по корню пропускает окно, когда PDB ещё MOUNTED,
                        # и первый же клиент получает ORA-65024.
                        cur.execute(f"SELECT open_mode FROM v$pdbs WHERE UPPER(name) = '{SYDAFA_DB.upper()}'")
                        pdb = cur.fetchone()
                        conn.close()
                        # Отсутствие PDB законно только при первичном bootstrap —
                        # её создаст provisioning; иначе ждём READ WRITE.
                        if row and row[0] == "READ WRITE" and (pdb is None or pdb[0] == "READ WRITE"):
                            return
                        if pdb is not None:
                            last_err = RuntimeError(f"PDB {SYDAFA_DB} open_mode={pdb[0]}")
                    except Exception as sys_err:
                        last_err = sys_err
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


def build_connection_details(
    source_name: str,
    config: SourceConfig,
    request_host: str | None = None,
) -> ConnectionDetails:
    is_external = bool(config.external_port)
    port = config.external_port if is_external else config.port
    db_host = container_name_for(config)
    host = "localhost" if is_external else (request_host or db_host)

    if config.type == "mssql":
        jdbc_url = f"jdbc:sqlserver://{host}:{port};databaseName={SYDAFA_DB};trustServerCertificate=true;encrypt=false"
    elif config.type == "oracle":
        jdbc_url = f"jdbc:oracle:thin:@//{host}:{port}/{SYDAFA_DB}"
    else:
        jdbc_url = f"jdbc:{config.type}://{host}:{port}/{SYDAFA_DB}"

    return ConnectionDetails(
        type=config.type,
        host=host,
        port=port,
        database=SYDAFA_DB,
        user=READER_USER,
        password=READER_PASSWORD,
        jdbc_url=jdbc_url,
    )


def _run_container(
    client: docker.DockerClient,
    source_name: str,
    config: SourceConfig,
) -> Any:
    image_name = SOURCE_IMAGES.get(config.type)
    if not image_name:
        raise ValueError(f"Неизвестный тип СУБД '{config.type}' для источника '{source_name}'")

    network_name, project_name = _get_context(client)
    name = container_name_for(config)
    volume_name = volume_name_for(config)
    data_path = DATA_VOLUME_PATHS[config.type]
    _ensure_volume(client, volume_name)

    secrets = get_secrets()
    labels = {
        "com.docker.compose.project": project_name,
        "com.docker.compose.service": name,
        "sydafa.managed": "true",
        "sydafa.prefix": CONTAINER_PREFIX,
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
    if config.external_port:
        ports = {f"{config.port}/tcp": config.external_port}

    logger.info(
        "Создание контейнера %s (образ %s, volume %s, ports=%s)...",
        name,
        image_name,
        volume_name,
        ports,
    )
    container = client.containers.run(
        image=image_name,
        name=name,
        hostname=name,
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

        timeout = 180 if config.type == "oracle" else 90
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
            vol.remove(force=True)
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
