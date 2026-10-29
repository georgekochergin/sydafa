"""Композиция backend источника MSSQL."""
from __future__ import annotations

from ....constants import READER_PASSWORD, READER_USER, SYDAFA_DB
from ....models import ConnectionDetails
from .dialect import MssqlDialect
from .runtime import MssqlRuntime


class MssqlBackend:
    engine_type = "mssql"
    family = "rdbms"
    internal_port = 1433

    def __init__(self) -> None:
        self.dialect = MssqlDialect()
        self.runtime = MssqlRuntime()

    def describe_connection(self, host: str, port: int) -> ConnectionDetails:
        return ConnectionDetails(
            type=self.engine_type,
            host=host,
            port=port,
            database=SYDAFA_DB,
            user=READER_USER,
            password=READER_PASSWORD,
            jdbc_url=(
                f"jdbc:sqlserver://{host}:{port};databaseName={SYDAFA_DB};"
                "trustServerCertificate=true;encrypt=false"
            ),
        )


__all__ = ["MssqlBackend", "MssqlRuntime"]
