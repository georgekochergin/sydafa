"""Runtime Oracle: образ/env, readiness (CDB/PDB) и идемпотентный bootstrap.

Переносится дословно из container_manager._wait_for_db_ready (oracle-ветка)
и provisioning.provision_oracle/_ensure_oracle_reader; порядок fallback,
переходные ORA-65054/65024 и тексты сохранены. Драйвер импортируется лениво.
"""
from __future__ import annotations

import logging
import os
import time

from ....constants import READER_USER, SYDAFA_DB
from ....models import SourceConfig
from ....secrets import AppSecrets
from ...contracts import ContainerSpec, ReadyContext, ReadyResult

logger = logging.getLogger("sydafa.backends.oracle.runtime")

DEFAULT_IMAGE = "gvenzl/oracle-free:23-slim-faststart"
DATA_PATH = "/opt/oracle/oradata"

STARTUP_TIMEOUT_SECONDS = 180


def _default_image() -> str:
    return os.environ.get("SYDAFA_ORACLE_IMAGE", DEFAULT_IMAGE)


def _is_pdb_open_in_progress(exc: Exception) -> bool:
    """True, если ошибка ALTER PLUGGABLE DATABASE OPEN означает переходное состояние PDB.

    PDB может открываться сама или другой сессией: ORA-65054/ORA-65024 и
    "already open" уходят в ожидание готовности сервиса ниже, а не наружу.
    """
    text = str(exc)
    return any(marker in text for marker in ("65054", "65024")) or "already open" in text.lower()


def _connect(dsn, user: str, password: str, mode: int = 0):
    import oracledb

    return oracledb.connect(user=user, password=password, dsn=dsn, mode=mode)


class OracleRuntime:
    startup_timeout = STARTUP_TIMEOUT_SECONDS

    def build_container_spec(self, config: SourceConfig, secrets: AppSecrets) -> ContainerSpec:
        return ContainerSpec(
            image=_default_image(),
            environment={"ORACLE_PASSWORD": secrets.oracle_sys_password},
            data_path=DATA_PATH,
        )

    def probe_ready(self, ctx: ReadyContext) -> ReadyResult:
        """Одна попытка: dba_user к PDB sydafa, иначе SYS к CDB + проверка CDB/PDB READ WRITE.

        Корень CDB открывается раньше PDB: готовность только по корню пропускает
        окно, когда PDB ещё MOUNTED, и первый же клиент получает ORA-65024.
        Отсутствие PDB законно только при первичном bootstrap — её создаст
        provisioning; иначе ждём READ WRITE.
        """
        import oracledb

        host, port = ctx.endpoint_host, ctx.config.port
        try:
            dsn = oracledb.makedsn(host, port, service_name=SYDAFA_DB)
            conn = _connect(dsn, ctx.secrets.dba_user, ctx.secrets.dba_password)
            conn.close()
            return ReadyResult(ready=True)
        except Exception:
            pass
        try:
            dsn = oracledb.makedsn(host, port, service_name="FREE")
            conn = _connect(dsn, "SYS", ctx.secrets.oracle_sys_password, mode=oracledb.AUTH_MODE_SYSDBA)
            cur = conn.cursor()
            cur.execute("SELECT open_mode FROM v$database")
            row = cur.fetchone()
            cur.execute(f"SELECT open_mode FROM v$pdbs WHERE UPPER(name) = '{SYDAFA_DB.upper()}'")
            pdb = cur.fetchone()
            conn.close()
            if row and row[0] == "READ WRITE" and (pdb is None or pdb[0] == "READ WRITE"):
                return ReadyResult(ready=True)
            if pdb is not None:
                return ReadyResult(ready=False, reason=f"PDB {SYDAFA_DB} open_mode={pdb[0]}")
            return ReadyResult(ready=False, reason=f"CDB open_mode={row[0] if row else 'unknown'}")
        except Exception as sys_err:
            return ReadyResult(ready=False, reason=str(sys_err))

    def provision(self, ctx: ReadyContext) -> None:
        import oracledb

        config, secrets = ctx.config, ctx.secrets
        reader_user = config.reader or READER_USER
        endpoint = ctx.endpoint_host
        logger.info("Provisioning Oracle источника '%s'...", config.name)

        if self._working_ready(endpoint, config.port, secrets):
            self._ensure_reader(endpoint, config.port, secrets, reader_user)
            logger.info("Oracle источник '%s' уже provisioned.", config.name)
            return

        dsn = oracledb.makedsn(config.host, config.port, service_name="FREE")
        conn = _connect(dsn, "SYS", secrets.oracle_sys_password, mode=oracledb.AUTH_MODE_SYSDBA)
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
                    try:
                        cur.execute(f"ALTER PLUGGABLE DATABASE {SYDAFA_DB} OPEN")
                    except Exception as e:
                        if _is_pdb_open_in_progress(e):
                            logger.info("PDB '%s' ещё открывается, ожидаем готовности сервиса (%s)", SYDAFA_DB, e)
                        else:
                            raise
        finally:
            conn.close()

        pdb_dsn = oracledb.makedsn(config.host, config.port, service_name=SYDAFA_DB)
        deadline = time.time() + 90
        conn = None
        last_pdb_err = None
        while time.time() < deadline:
            try:
                conn = _connect(pdb_dsn, "SYS", secrets.oracle_sys_password, mode=oracledb.AUTH_MODE_SYSDBA)
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
                logger.info("Oracle источник '%s': пользователь pdbadmin удалён.", config.name)
        except Exception as e:
            logger.error("Ошибка настройки PDB %s: %s", SYDAFA_DB, e, exc_info=True)
            raise
        finally:
            conn.close()

        self._ensure_reader(endpoint, config.port, secrets, reader_user)
        logger.info(
            "Oracle источник '%s': суперпользователь '%s', PDB '%s' и reader '%s' настроены.",
            config.name,
            secrets.dba_user,
            SYDAFA_DB,
            reader_user,
        )

    @staticmethod
    def _working_ready(host: str, port: int, secrets: AppSecrets) -> bool:
        import oracledb

        try:
            dsn = oracledb.makedsn(host, port, service_name=SYDAFA_DB)
            conn = _connect(dsn, secrets.dba_user, secrets.dba_password)
            conn.close()
            return True
        except Exception:
            return False

    @staticmethod
    def _ensure_reader(host: str, port: int, secrets: AppSecrets, reader_user: str) -> None:
        """Создаёт/проверяет публичного читателя в PDB и выдаёт ему права.

        Помимо чтения данных (SELECT ANY TABLE) читатель получает SELECT ANY
        DICTIONARY — только для мониторинга активных сессий (v$session).
        Выдача выполняется из сессии SYSDBA: у dba_user есть SYSDBA
        CONTAINER=ALL, а обычная сессия с ролью DBA не может выдать системную
        привилегию словаря.
        """
        import oracledb

        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                dsn = oracledb.makedsn(host, port, service_name=SYDAFA_DB)
                conn = _connect(dsn, "SYS", secrets.oracle_sys_password, mode=oracledb.AUTH_MODE_SYSDBA)
                try:
                    cur = conn.cursor()
                    cur.execute(f"SELECT username FROM all_users WHERE UPPER(username) = UPPER('{reader_user}')")
                    if not cur.fetchone():
                        cur.execute(f'CREATE USER {reader_user} IDENTIFIED BY "{reader_user}"')
                    cur.execute(f"GRANT CREATE SESSION TO {reader_user}")
                    cur.execute(f"GRANT SELECT ANY TABLE TO {reader_user}")
                    cur.execute(f"GRANT SELECT ANY DICTIONARY TO {reader_user}")
                finally:
                    conn.close()
                return
            except Exception:
                time.sleep(2)
        raise TimeoutError(f"Не удалось настроить пользователя {reader_user} в Oracle {SYDAFA_DB}")
