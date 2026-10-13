"""Подключения к источникам данных под суперпользователем sydafa."""
from __future__ import annotations

import logging
from contextlib import contextmanager

from .constants import SYDAFA_DB, SYDAFA_USER
from .secrets import get_secrets
from .sources import get_source

logger = logging.getLogger("sydafa.db_connectors")


@contextmanager
def mssql_connection(source_name: str = "mssql", database: str | None = None, autocommit: bool = False):
    import pymssql
    cfg = get_source(source_name)
    target_db = database or SYDAFA_DB
    password = get_secrets().sydafa_password
    try:
        conn = pymssql.connect(
            server=cfg.host,
            port=cfg.port,
            user=SYDAFA_USER,
            password=password,
            database=target_db,
            autocommit=autocommit,
        )
    except Exception as e:
        msg = f"Ошибка подключения к MSSQL источнику '{source_name}' ({cfg.host}:{cfg.port}/{target_db}) под {SYDAFA_USER}: {e}"
        logger.error(msg)
        raise RuntimeError(msg) from e

    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def oracle_connection(source_name: str = "oracle", service_name: str | None = None, mode: int = 0):
    import oracledb
    cfg = get_source(source_name)
    target_service = service_name or SYDAFA_DB
    dsn = oracledb.makedsn(cfg.host, cfg.port, service_name=target_service)
    password = get_secrets().sydafa_password
    try:
        conn = oracledb.connect(
            user=SYDAFA_USER,
            password=password,
            dsn=dsn,
            mode=mode,
        )
    except Exception as e:
        msg = f"Ошибка подключения к Oracle источнику '{source_name}' ({cfg.host}:{cfg.port}/{target_service}) под {SYDAFA_USER}: {e}"
        logger.error(msg)
        raise RuntimeError(msg) from e

    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_connection(source_name: str, database: str | None = None, autocommit: bool = False):
    cfg = get_source(source_name)
    if cfg.type == "mssql":
        with mssql_connection(source_name=source_name, database=database, autocommit=autocommit) as conn:
            yield conn
    elif cfg.type == "oracle":
        with oracle_connection(source_name=source_name, service_name=database) as conn:
            yield conn
    else:
        raise ValueError(f"Неподдерживаемый тип СУБД '{cfg.type}' для источника '{source_name}'")
