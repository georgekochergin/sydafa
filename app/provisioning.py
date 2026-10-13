"""Идемпотентный provisioning рабочей базы sydafa и пользователя reader."""
from __future__ import annotations

import logging
import time

from .constants import READER_PASSWORD, READER_USER, SYDAFA_DB
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


def _ensure_mssql_reader(source_name: str, reader_user: str = READER_USER) -> None:
    users_to_create = {reader_user: reader_user}
    if reader_user == READER_USER:
        users_to_create["reader"] = "reader"

    with mssql_connection(source_name=source_name, database=SYDAFA_DB, autocommit=True) as conn:
        cur = conn.cursor()
        for u, p in users_to_create.items():
            cur.execute(
                f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{u}') "
                f"CREATE LOGIN [{u}] WITH PASSWORD = '{p}', CHECK_POLICY = OFF, CHECK_EXPIRATION = OFF"
            )
            cur.execute(
                f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '{u}') "
                f"CREATE USER [{u}] FOR LOGIN [{u}]"
            )
            cur.execute(f"ALTER ROLE db_datareader ADD MEMBER [{u}]")


def provision_mssql(source_name: str, config: SourceConfig) -> None:
    import pymssql

    secrets = get_secrets()
    reader_user = config.reader or READER_USER
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
            f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{secrets.dba_user}') "
            f"BEGIN "
            f"    CREATE LOGIN [{secrets.dba_user}] WITH PASSWORD = '{secrets.dba_password}', CHECK_POLICY = OFF; "
            f"    ALTER SERVER ROLE sysadmin ADD MEMBER [{secrets.dba_user}]; "
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
        secrets.dba_user,
        SYDAFA_DB,
        reader_user,
    )


def _ensure_oracle_reader(source_name: str, reader_user: str = READER_USER) -> None:
    import time
    users_to_create = [reader_user]
    if reader_user != "reader" and "reader" not in users_to_create:
        users_to_create.append("reader")

    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with oracle_connection(source_name=source_name, service_name=SYDAFA_DB) as conn:
                cur = conn.cursor()
                for u in users_to_create:
                    cur.execute(f"SELECT username FROM all_users WHERE UPPER(username) = UPPER('{u}')")
                    if not cur.fetchone():
                        cur.execute(f'CREATE USER {u} IDENTIFIED BY "{u}"')
                    cur.execute(f"GRANT CREATE SESSION TO {u}")
                    cur.execute(f"GRANT SELECT ANY TABLE TO {u}")
                return
        except Exception:
            time.sleep(2)
    raise TimeoutError(f"Не удалось настроить reader пользователей в Oracle {SYDAFA_DB}")


def provision_oracle(source_name: str, config: SourceConfig) -> None:
    import oracledb

    secrets = get_secrets()
    reader_user = config.reader or READER_USER
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
        cur.execute(f"SELECT username FROM all_users WHERE UPPER(username) = UPPER('{secrets.dba_user}')")
        if not cur.fetchone():
            logger.info("Создание суперпользователя '%s' в Oracle CDB...", secrets.dba_user)
            cur.execute("ALTER SESSION SET \"_ORACLE_SCRIPT\"=true")
            cur.execute(f'CREATE USER "{secrets.dba_user}" IDENTIFIED BY "{secrets.dba_password}" CONTAINER=ALL')
            cur.execute(f'GRANT DBA, SYSDBA TO "{secrets.dba_user}" CONTAINER=ALL')

        cur.execute("SELECT name, open_mode FROM v$pdbs WHERE UPPER(name) = 'SYDAFA'")
        row = cur.fetchone()
        if not row:
            logger.info("Создание PDB '%s'...", SYDAFA_DB)
            cur.execute(
                f"CREATE PLUGGABLE DATABASE {SYDAFA_DB} ADMIN USER pdbadmin IDENTIFIED BY \"{secrets.dba_password}\" "
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
    deadline = time.time() + 90
    conn = None
    last_pdb_err = None
    while time.time() < deadline:
        try:
            conn = oracledb.connect(
                user="SYS",
                password=secrets.oracle_sys_password,
                dsn=pdb_dsn,
                mode=oracledb.AUTH_MODE_SYSDBA,
            )
            break
        except Exception as e:
            last_pdb_err = e
            time.sleep(2)
    if conn is None:
        raise TimeoutError(f"Таймаут ожидания регистрации PDB сервиса '{SYDAFA_DB}' в listener: {last_pdb_err}")

    try:
        cur = conn.cursor()
        cur.execute("ALTER SESSION SET \"_ORACLE_SCRIPT\"=true")
        cur.execute(f"SELECT username FROM all_users WHERE UPPER(username) = UPPER('{secrets.dba_user}')")
        if not cur.fetchone():
            logger.info("Создание суперпользователя '%s' в PDB %s...", secrets.dba_user, SYDAFA_DB)
            cur.execute(f'CREATE USER "{secrets.dba_user}" IDENTIFIED BY "{secrets.dba_password}"')
        else:
            cur.execute(f'ALTER USER "{secrets.dba_user}" IDENTIFIED BY "{secrets.dba_password}"')
        cur.execute(f'GRANT DBA, SYSDBA, UNLIMITED TABLESPACE TO "{secrets.dba_user}"')

        cur.execute("SELECT username FROM all_users WHERE UPPER(username) = 'PDBADMIN'")
        if cur.fetchone():
            cur.execute("DROP USER pdbadmin CASCADE")
            logger.info("Oracle источник '%s': пользователь pdbadmin удалён.", source_name)
    except Exception as e:
        logger.error("Ошибка настройки PDB %s: %s", SYDAFA_DB, e, exc_info=True)
        raise
    finally:
        conn.close()

    _ensure_oracle_reader(source_name, reader_user)
    logger.info(
        "Oracle источник '%s': суперпользователь '%s', PDB '%s' и reader '%s' настроены.",
        source_name,
        secrets.dba_user,
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
