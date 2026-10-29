"""Общие реляционные правила схемы: ключи, аудит-таблицы и их модели.

Принадлежит общему RDBMS-слою: не зависит от движка; SQL-рендеринг
выполняет диалект, обновление LiveSchema — вызывающий слой.
"""
from __future__ import annotations

from ...models import EXEC_BATCH_ID_COLUMN, ColumnSpec, TableSchema


def table_key(schema: str, table: str) -> str:
    return f"{schema}.{table}"


def upd_table_name(table_name: str) -> str:
    return f"_upd_{table_name}"


def del_table_name(table_name: str) -> str:
    return f"_del_{table_name}"


def make_audit_table_schema(main_table: TableSchema, audit_name: str) -> TableSchema:
    """Аудит-таблица _upd_/_del_: _exec_batch_id + колонки основной таблицы, без PK/FK."""
    audit_columns = [ColumnSpec(name=EXEC_BATCH_ID_COLUMN, type="bigint")]
    for col in main_table.columns:
        if col.name != EXEC_BATCH_ID_COLUMN:
            audit_columns.append(col.model_copy())
    return TableSchema(
        name=audit_name,
        schema=main_table.schema_,
        primary_key=[],
        columns=audit_columns,
        foreign_keys=[],
    )


def recreate_audit_tables_ddl(
    dialect,
    table_obj: TableSchema,
    live,
) -> list[str]:
    """DROP + CREATE аудит-таблиц _upd_/_del_ при изменении колонок основной таблицы.

    Обновляет live-модель новыми схемами аудит-таблиц.
    """
    upd_name = upd_table_name(table_obj.name)
    del_name = del_table_name(table_obj.name)
    ddl = [
        dialect.drop_table(table_obj.schema_, upd_name),
        dialect.drop_table(table_obj.schema_, del_name),
    ]
    upd_schema = make_audit_table_schema(table_obj, upd_name)
    del_schema = make_audit_table_schema(table_obj, del_name)
    ddl.append(dialect.create_table(upd_schema))
    ddl.append(dialect.create_table(del_schema))

    live.tables[table_key(table_obj.schema_, upd_name)] = upd_schema
    live.tables[table_key(table_obj.schema_, del_name)] = del_schema
    return ddl
