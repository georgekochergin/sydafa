"""Runtime MSSQL: образ/env, readiness и идемпотентный bootstrap.

Переносится дословно из container_manager._wait_for_db_ready (mssql-ветка)
и provisioning.provision_mssql; порядок fallback и тексты сохранены.
Драйвер импортируется лениво — только при реальном подключении.
"""
from __future__ import annotations

import logging
import os

from ....constants import READER_USER, SYDAFA_DB
from ....models import SourceConfig
from ....secrets import AppSecrets
from ...contracts import ContainerSpec, ReadyContext, ReadyResult

logger = logging.getLogger("sydafa.backends.mssql.runtime")

DEFAULT_IMAGE = "mcr.microsoft.com/mssql/server:2022-CU27-ubuntu-22.04"
DATA_PATH = "/var/opt/mssql"

STARTUP_TIMEOUT_SECONDS = 90


def _default_image() -> str:
    return os.environ.get("SYDAFA_MSSQL_IMAGE", DEFAULT_IMAGE)


def _connect(host: str, port: int, user: str, password: str, database: str, autocommit: bool, login_timeout: int | None = None):
    import pymssql

    kwargs: dict = {
        "server": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "autocommit": autocommit,
    }
    if login_timeout is not None:
        kwargs["login_timeout"] = login_timeout
    return pymssql.connect(**kwargs)


class MssqlRuntime:
    startup_timeout = STARTUP_TIMEOUT_SECONDS

    def build_container_spec(self, config: SourceConfig, secrets: AppSecrets) -> ContainerSpec:
        return ContainerSpec(
            image=_default_image(),
            environment={
                "ACCEPT_EULA": "Y",
                "MSSQL_SA_PASSWORD": secrets.mssql_sa_password,
                "MSSQL_PID": "Developer",
            },
            data_path=DATA_PATH,
        )

    def probe_ready(self, ctx: ReadyContext) -> ReadyResult:
        """Одна попытка: dba_user к рабочей БД, иначе sa к master (bootstrap)."""
        host, port = ctx.endpoint_host, ctx.config.port
        try:
            conn = _connect(
                host, port,
                ctx.secrets.dba_user, ctx.secrets.dba_password,
                database=SYDAFA_DB, autocommit=False, login_timeout=3,
            )
            conn.close()
            return ReadyResult(ready=True)
        except Exception as e:
            try:
                conn = _connect(
                    host, port,
                    "sa", ctx.secrets.mssql_sa_password,
                    database="master", autocommit=False, login_timeout=3,
                )
                conn.close()
                return ReadyResult(ready=True)
            except Exception as sa_err:
                return ReadyResult(ready=False, reason=str(sa_err))

    def provision(self, ctx: ReadyContext) -> None:
        from contextlib import contextmanager

        config, secrets = ctx.config, ctx.secrets
        reader_user = config.reader or READER_USER
        endpoint = ctx.endpoint_host
        logger.info("Provisioning MSSQL источника '%s'...", config.name)

        @contextmanager
        def _working(autocommit: bool = True):
            conn = _connect(endpoint, config.port, secrets.dba_user, secrets.dba_password,
                            database=SYDAFA_DB, autocommit=autocommit)
            try:
                yield conn
            finally:
                conn.close()

        def _ready() -> bool:
            try:
                with _working():
                    return True
            except Exception:
                return False

        def _ensure_reader() -> None:
            with _working() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{reader_user}') "
                    f"CREATE LOGIN [{reader_user}] WITH PASSWORD = '{reader_user}', CHECK_POLICY = OFF, CHECK_EXPIRATION = OFF"
                )
                cur.execute(
                    f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '{reader_user}') "
                    f"CREATE USER [{reader_user}] FOR LOGIN [{reader_user}]"
                )
                cur.execute(f"ALTER ROLE db_datareader ADD MEMBER [{reader_user}]")

        if _ready():
            _ensure_reader()
            logger.info("MSSQL источник '%s' уже provisioned.", config.name)
            return

        conn = _connect(config.host, config.port, "sa", secrets.mssql_sa_password,
                        database="master", autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute(
                f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{secrets.dba_user}') "
                f"BEGIN "
                f"    CREATE LOGIN [{secrets.dba_user}] WITH PASSWORD = '{secrets.dba_password}', CHECK_POLICY = OFF; "
                f"    ALTER SERVER ROLE sysadmin ADD MEMBER [{secrets.dba_user}]; "
                f"END"
            )
            cur.execute(f"IF DB_ID('{SYDAFA_DB}') IS NULL CREATE DATABASE [{SYDAFA_DB}]")
        finally:
            conn.close()

        _ensure_reader()

        conn = _connect(config.host, config.port, "sa", secrets.mssql_sa_password,
                        database="master", autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute("ALTER LOGIN [sa] DISABLE")
            logger.info("MSSQL источник '%s': login sa отключён.", config.name)
        finally:
            conn.close()

        logger.info(
            "MSSQL источник '%s': суперпользователь '%s', БД '%s' и reader '%s' настроены.",
            config.name,
            secrets.dba_user,
            SYDAFA_DB,
            reader_user,
        )
