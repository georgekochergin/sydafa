"""Sydafa Airflow operators for DBMS source lifecycle and migrations."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from airflow.models import BaseOperator

from sydafa_airflow.hooks.sydafa import SydafaHook

if TYPE_CHECKING:
    from airflow.utils.context import Context


class SydafaActivateOperator(BaseOperator):
    """Оператор явного пробуждения (Lazy Start) источника данных в sydafa.

    Отправляет запрос POST /sources/{source}/activate?ttl_minutes={ttl_minutes}.
    Гарантирует, что СУБД запущена, порт открыт, и выставляет актуальный TTL.

    Параметр `conn_id` является строго обязательным.
    """

    template_fields: Sequence[str] = ("source", "ttl_minutes", "conn_id")

    def __init__(
        self,
        *,
        conn_id: str,
        source: str,
        ttl_minutes: int = 15,
        timeout_seconds: int = 180,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not conn_id:
            raise ValueError("Параметр 'conn_id' обязателен для SydafaActivateOperator")
        self.conn_id = conn_id
        self.source = source
        self.ttl_minutes = ttl_minutes
        self.timeout_seconds = timeout_seconds

    def execute(self, context: Context) -> dict[str, Any]:
        hook = SydafaHook(conn_id=self.conn_id)
        return hook.activate(
            source=self.source,
            ttl_minutes=self.ttl_minutes,
            timeout_seconds=self.timeout_seconds,
        )


class SydafaMigrateOperator(BaseOperator):
    """Оператор транзакционной эволюции схем и генерации данных в sydafa.

    Отправляет запрос POST /sources/{source}/migrate с батчем DDL и DML операций.
    Выполняет транзакцию All-or-Nothing.

    Параметр `conn_id` является строго обязательным.
    """

    template_fields: Sequence[str] = ("source", "operations", "conn_id")
    template_fields_renderers = {"operations": "json"}

    def __init__(
        self,
        *,
        conn_id: str,
        source: str,
        operations: list[dict[str, Any]],
        timeout_seconds: int = 300,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not conn_id:
            raise ValueError("Параметр 'conn_id' обязателен для SydafaMigrateOperator")
        self.conn_id = conn_id
        self.source = source
        self.operations = operations
        self.timeout_seconds = timeout_seconds

    def execute(self, context: Context) -> dict[str, Any]:
        hook = SydafaHook(conn_id=self.conn_id)
        return hook.migrate(
            source=self.source,
            operations=self.operations,
            timeout_seconds=self.timeout_seconds,
        )


class SydafaDeactivateOperator(BaseOperator):
    """Оператор принудительной деактивации (остановки) СУБД источника в sydafa.

    Отправляет запрос POST /sources/{source}/deactivate.
    Останавливает контейнер СУБД для освобождения RAM на хосте.

    Параметр `conn_id` является строго обязательным.
    """

    template_fields: Sequence[str] = ("source", "conn_id")

    def __init__(
        self,
        *,
        conn_id: str,
        source: str,
        timeout_seconds: int = 60,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not conn_id:
            raise ValueError("Параметр 'conn_id' обязателен для SydafaDeactivateOperator")
        self.conn_id = conn_id
        self.source = source
        self.timeout_seconds = timeout_seconds

    def execute(self, context: Context) -> dict[str, Any]:
        hook = SydafaHook(conn_id=self.conn_id)
        return hook.deactivate(
            source=self.source,
            timeout_seconds=self.timeout_seconds,
        )
