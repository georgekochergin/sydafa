"""Композиция backend источника Oracle."""
from __future__ import annotations

from ....constants import READER_PASSWORD, READER_USER, SYDAFA_DB
from ....models import ConnectionDetails
from .dialect import OracleDialect
from .runtime import OracleRuntime


class OracleBackend:
    engine_type = "oracle"
    family = "rdbms"
    internal_port = 1521

    def __init__(self) -> None:
        self.dialect = OracleDialect()
        self.runtime = OracleRuntime()

    def describe_connection(self, host: str, port: int) -> ConnectionDetails:
        return ConnectionDetails(
            type=self.engine_type,
            host=host,
            port=port,
            database=SYDAFA_DB,
            user=READER_USER,
            password=READER_PASSWORD,
            jdbc_url=f"jdbc:oracle:thin:@//{host}:{port}/{SYDAFA_DB}",
        )


__all__ = ["OracleBackend", "OracleRuntime"]
