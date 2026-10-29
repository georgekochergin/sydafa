"""Рабочие session MSSQL (DB-API pymssql) под внутренней DBA-учёткой.

Единая точка `open_working` для всех сценариев исполнения: bootstrap-подключения
(master/sa) закрыты внутри runtime и сюда не входят. Host-резолвенция — по
имени контейнера, переданному вызывающим слоем (D-9: адресация не меняется).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from ....constants import SYDAFA_DB
from ....models import SourceConfig
from ....secrets import AppSecrets

logger = logging.getLogger("sydafa.backends.mssql.connection")


@contextmanager
def open_working(
    config: SourceConfig,
    secrets: AppSecrets,
    endpoint_host: str,
    autocommit: bool = False,
):
    """Рабочая session к БД {SYDAFA_DB} под dba_user; закрытие гарантировано."""
    import pymssql

    target_db = SYDAFA_DB
    try:
        conn = pymssql.connect(
            server=endpoint_host,
            port=config.port,
            user=secrets.dba_user,
            password=secrets.dba_password,
            database=target_db,
            autocommit=autocommit,
        )
    except Exception as e:
        msg = (
            f"Ошибка подключения к MSSQL источнику '{config.name}' "
            f"({endpoint_host}:{config.port}/{target_db}) под {secrets.dba_user}: {e}"
        )
        logger.error(msg)
        raise RuntimeError(msg) from e

    try:
        yield conn
    finally:
        conn.close()
