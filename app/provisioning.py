"""Идемпотентный provisioning рабочей базы sydafa и пользователя reader/reader."""
from __future__ import annotations

import logging
import os
from .constants import SYDAFA_DB, SYDAFA_PASSWORD, SYDAFA_USER
from .db_connectors import mssql_connection, oracle_connection
from .models import SourceConfig
from .sources import all_sources, get_source

logger = logging.getLogger("sydafa.provisioning")


def provision_mssql(source_name: str, config: SourceConfig) -> None:
    """Создаёт суперпользователя sydafa, БД sydafa и пользователя reader/reader в MSSQL."""
    import pymssql
    sa_password = os.environ.get("MSSQL_SA_PASSWORD", "AdminPassword1!")
    logger.info("Provisioning MSSQL источника '%s'...", source_name)

    # 1. Первичное подключение под sa к master для создания sydafa/sydafa и базы sydafa
    conn = pymssql.connect(
        server=config.host,
        port=config.port,
        user="sa",
        password=sa_password,
        database="master",
        autocommit=True,
    )
    try:
        cur = conn.cursor()
        cur.execute(
            f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{SYDAFA_USER}') "
            f"BEGIN "
            f"    CREATE LOGIN [{SYDAFA_USER}] WITH PASSWORD = '{SYDAFA_PASSWORD}', CHECK_POLICY = OFF; "
            f"    ALTER SERVER ROLE sysadmin ADD MEMBER [{SYDAFA_USER}]; "
            f"END"
        )
        cur.execute(f"IF DB_ID('{SYDAFA_DB}') IS NULL CREATE DATABASE [{SYDAFA_DB}]")
    finally:
        conn.close()

    # 2. Создание логина и пользователя reader в БД sydafa
    reader_user = config.reader
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

    logger.info("MSSQL источник '%s': суперпользователь '%s', БД '%s' и reader '%s' успешно настроены.", source_name, SYDAFA_USER, SYDAFA_DB, reader_user)


def provision_oracle(source_name: str, config: SourceConfig) -> None:
    """Создаёт суперпользователя sydafa, PDB sydafa и пользователя reader/reader в Oracle."""
    import oracledb
    oracle_password = os.environ.get("ORACLE_PASSWORD", "admin")
    logger.info("Provisioning Oracle источника '%s'...", source_name)

    # 1. Первичное подключение под SYS к FREE для создания sydafa и PDB sydafa
    dsn = oracledb.makedsn(config.host, config.port, service_name="FREE")
    conn = oracledb.connect(
        user="SYS",
        password=oracle_password,
        dsn=dsn,
        mode=oracledb.AUTH_MODE_SYSDBA,
    )
    try:
        cur = conn.cursor()
        # Проверяем и создаем суперпользователя sydafa в CDB
        cur.execute(f"SELECT username FROM all_users WHERE UPPER(username) = UPPER('{SYDAFA_USER}')")
        if not cur.fetchone():
            logger.info("Создание суперпользователя '%s' в Oracle CDB...", SYDAFA_USER)
            cur.execute(f"CREATE USER {SYDAFA_USER} IDENTIFIED BY {SYDAFA_PASSWORD} CONTAINER=ALL")
            cur.execute(f"GRANT DBA, SYSDBA TO {SYDAFA_USER} CONTAINER=ALL")

        # Проверяем и создаем PDB sydafa
        cur.execute("SELECT name, open_mode FROM v$pdbs WHERE UPPER(name) = 'SYDAFA'")
        row = cur.fetchone()
        if not row:
            logger.info("Создание PDB '%s'...", SYDAFA_DB)
            cur.execute(
                f"CREATE PLUGGABLE DATABASE {SYDAFA_DB} ADMIN USER pdbadmin IDENTIFIED BY sydafa "
                f"FILE_NAME_CONVERT=('pdbseed', '{SYDAFA_DB}')"
            )
            cur.execute(f"ALTER PLUGGABLE DATABASE {SYDAFA_DB} OPEN")
            cur.execute(f"ALTER PLUGGABLE DATABASE {SYDAFA_DB} SAVE STATE")
        else:
            open_mode = row[1]
            if open_mode == "MOUNTED":
                logger.info("Открытие PDB '%s'...", SYDAFA_DB)
                cur.execute(f"ALTER PLUGGABLE DATABASE {SYDAFA_DB} OPEN")
    finally:
        conn.close()

    # 2. Подключение к PDB sydafa под sydafa/sydafa и создание пользователя reader
    reader_user = config.reader
    with oracle_connection(source_name=source_name, service_name=SYDAFA_DB) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT username FROM all_users WHERE UPPER(username) = UPPER('{reader_user}')")
        if not cur.fetchone():
            logger.info("Создание пользователя reader '%s' в PDB '%s'...", reader_user, SYDAFA_DB)
            cur.execute(f'CREATE USER {reader_user} IDENTIFIED BY "{reader_user}"')

        cur.execute(f"GRANT CREATE SESSION TO {reader_user}")
        cur.execute(f"GRANT SELECT ANY TABLE TO {reader_user}")

    logger.info("Oracle источник '%s': суперпользователь '%s', PDB '%s' и reader '%s' успешно настроены.", source_name, SYDAFA_USER, SYDAFA_DB, reader_user)


def provision_source(source_name: str) -> None:
    """Выполняет provisioning для конкретного источника."""
    config = get_source(source_name)
    if config.type == "mssql":
        provision_mssql(source_name, config)
    elif config.type == "oracle":
        provision_oracle(source_name, config)
    else:
        raise ValueError(f"Неизвестный тип СУБД '{config.type}' для источника '{source_name}'")


def provision_all_sources() -> None:
    """Выполняет provisioning всех источников из конфигурации."""
    sources = all_sources()
    for name in sources:
        provision_source(name)
