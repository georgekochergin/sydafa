"""Идемпотентный provisioning рабочей базы sydafa и пользователя reader."""
from __future__ import annotations

import logging

from .constants import SYDAFA_DB, SYDAFA_USER
from .db_connectors import mssql_connection, oracle_connection
from .models import SourceConfig
from .secrets import get_secrets
from .sources import all_sources, get_source

logger = logging.getLogger("sydafa.provisioning")


def _sydafa_ready_mssql(source_name: str) -> bool:
    try:
        with mssql_connection(source_name=source_name, database=SYDAFA_DB, autocommit=True):
            return True
    except Exception:
        return False


def _sydafa_ready_oracle(source_name: str) -> bool:
    try:
        with oracle_connection(source_name=source_name, service_name=SYDAFA_DB):
            return True
    except Exception:
        return False


def _ensure_mssql_reader(source_name: str, reader_user: str) -> None:
    with mssql_connection(source_name=source_name, database=SYDAFA_DB, autocommit=True) as conn:
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


def provision_mssql(source_name: str, config: SourceConfig) -> None:
    import pymssql

    secrets = get_secrets()
    reader_user = config.reader
    logger.info("Provisioning MSSQL источника '%s'...", source_name)

    if _sydafa_ready_mssql(source_name):
        _ensure_mssql_reader(source_name, reader_user)
        logger.info("MSSQL источник '%s' уже provisioned.", source_name)
        return

    conn = pymssql.connect(
        server=config.host,
        port=config.port,
        user="sa",
        password=secrets.mssql_sa_password,
        database="master",
        autocommit=True,
    )
    try:
        cur = conn.cursor()
        cur.execute(
            f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{SYDAFA_USER}') "
            f"BEGIN "
            f"    CREATE LOGIN [{SYDAFA_USER}] WITH PASSWORD = '{secrets.sydafa_password}', CHECK_POLICY = OFF; "
            f"    ALTER SERVER ROLE sysadmin ADD MEMBER [{SYDAFA_USER}]; "
            f"END"
        )
        cur.execute(f"IF DB_ID('{SYDAFA_DB}') IS NULL CREATE DATABASE [{SYDAFA_DB}]")
    finally:
        conn.close()

    _ensure_mssql_reader(source_name, reader_user)

    conn = pymssql.connect(
        server=config.host,
        port=config.port,
        user="sa",
        password=secrets.mssql_sa_password,
        database="master",
        autocommit=True,
    )
    try:
        cur = conn.cursor()
        cur.execute("ALTER LOGIN [sa] DISABLE")
        logger.info("MSSQL источник '%s': login sa отключён.", source_name)
    finally:
        conn.close()

    logger.info(
        "MSSQL источник '%s': суперпользователь '%s', БД '%s' и reader '%s' настроены.",
        source_name,
        SYDAFA_USER,
        SYDAFA_DB,
        reader_user,
    )


def _ensure_oracle_reader(source_name: str, reader_user: str) -> None:
    with oracle_connection(source_name=source_name, service_name=SYDAFA_DB) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT username FROM all_users WHERE UPPER(username) = UPPER('{reader_user}')")
        if not cur.fetchone():
            cur.execute(f'CREATE USER {reader_user} IDENTIFIED BY "{reader_user}"')
        cur.execute(f"GRANT CREATE SESSION TO {reader_user}")
        cur.execute(f"GRANT SELECT ANY TABLE TO {reader_user}")


def provision_oracle(source_name: str, config: SourceConfig) -> None:
    import oracledb

    secrets = get_secrets()
    reader_user = config.reader
    logger.info("Provisioning Oracle источника '%s'...", source_name)

    if _sydafa_ready_oracle(source_name):
        _ensure_oracle_reader(source_name, reader_user)
        logger.info("Oracle источник '%s' уже provisioned.", source_name)
        return

    dsn = oracledb.makedsn(config.host, config.port, service_name="FREE")
    conn = oracledb.connect(
        user="SYS",
        password=secrets.oracle_sys_password,
        dsn=dsn,
        mode=oracledb.AUTH_MODE_SYSDBA,
    )
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT username FROM all_users WHERE UPPER(username) = UPPER('{SYDAFA_USER}')")
        if not cur.fetchone():
            logger.info("Создание суперпользователя '%s' в Oracle CDB...", SYDAFA_USER)
            cur.execute(f"CREATE USER {SYDAFA_USER} IDENTIFIED BY {secrets.sydafa_password} CONTAINER=ALL")
            cur.execute(f"GRANT DBA, SYSDBA TO {SYDAFA_USER} CONTAINER=ALL")

        cur.execute("SELECT name, open_mode FROM v$pdbs WHERE UPPER(name) = 'SYDAFA'")
        row = cur.fetchone()
        if not row:
            logger.info("Создание PDB '%s'...", SYDAFA_DB)
            cur.execute(
                f"CREATE PLUGGABLE DATABASE {SYDAFA_DB} ADMIN USER pdbadmin IDENTIFIED BY {secrets.sydafa_password} "
                f"FILE_NAME_CONVERT=('pdbseed', '{SYDAFA_DB}')"
            )
            cur.execute(f"ALTER PLUGGABLE DATABASE {SYDAFA_DB} OPEN")
            cur.execute(f"ALTER PLUGGABLE DATABASE {SYDAFA_DB} SAVE STATE")
        else:
            if row[1] == "MOUNTED":
                logger.info("Открытие PDB '%s'...", SYDAFA_DB)
                cur.execute(f"ALTER PLUGGABLE DATABASE {SYDAFA_DB} OPEN")
    finally:
        conn.close()

    pdb_dsn = oracledb.makedsn(config.host, config.port, service_name=SYDAFA_DB)
    conn = oracledb.connect(
        user="SYS",
        password=secrets.oracle_sys_password,
        dsn=pdb_dsn,
        mode=oracledb.AUTH_MODE_SYSDBA,
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT username FROM all_users WHERE UPPER(username) = 'PDBADMIN'")
        if cur.fetchone():
            cur.execute("DROP USER pdbadmin CASCADE")
            logger.info("Oracle источник '%s': пользователь pdbadmin удалён.", source_name)
    except Exception as e:
        logger.warning("Не удалось удалить pdbadmin: %s", e)
    finally:
        conn.close()

    _ensure_oracle_reader(source_name, reader_user)
    logger.info(
        "Oracle источник '%s': суперпользователь '%s', PDB '%s' и reader '%s' настроены.",
        source_name,
        SYDAFA_USER,
        SYDAFA_DB,
        reader_user,
    )


def provision_source(source_name: str) -> None:
    config = get_source(source_name)
    if config.type == "mssql":
        provision_mssql(source_name, config)
    elif config.type == "oracle":
        provision_oracle(source_name, config)
    else:
        raise ValueError(f"Неизвестный тип СУБД '{config.type}' для источника '{source_name}'")


def provision_all_sources() -> None:
    for name in all_sources():
        provision_source(name)
