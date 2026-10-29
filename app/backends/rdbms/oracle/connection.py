"""Рабочие session Oracle (oracledb) под внутренней DBA-учёткой.

Единая точка `open_working`: service_name всегда рабочая PDB sydafa;
bootstrap-подключения (SYS/FREE) закрыты внутри runtime. Host-резолвенция —
по имени контейнера, переданному вызывающим слоем (D-9).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from ....constants import SYDAFA_DB
from ....models import SourceConfig
from ....secrets import AppSecrets

logger = logging.getLogger("sydafa.backends.oracle.connection")


@contextmanager
def open_working(
    config: SourceConfig,
    secrets: AppSecrets,
    endpoint_host: str,
    autocommit: bool = False,
):
    """Рабочая session к PDB {SYDAFA_DB} под dba_user; закрытие гарантировано.

    oracledb по умолчанию не в autocommit — параметр принят для единства
    контракта и управляет явным commit/rollback вызывающего слоя.
    """
    import oracledb

    target_service = SYDAFA_DB
    dsn = oracledb.makedsn(endpoint_host, config.port, service_name=target_service)
    try:
        conn = oracledb.connect(
            user=secrets.dba_user,
            password=secrets.dba_password,
            dsn=dsn,
        )
    except Exception as e:
        msg = (
            f"Ошибка подключения к Oracle источнику '{config.name}' "
            f"({endpoint_host}:{config.port}/{target_service}) под {secrets.dba_user}: {e}"
        )
        logger.error(msg)
        raise RuntimeError(msg) from e

    try:
        yield conn
    finally:
        conn.close()
