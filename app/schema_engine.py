"""Движок транзакционного применения операций эволюции схемы (All-or-Nothing)."""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from .models import (
    AddColumnOp,
    BATCH_ID_COLUMN,
    ColumnSpec,
    CreateSchemaOp,
    CreateTableOp,
    DropColumnOp,
    DropSchemaOp,
    DropTableOp,
    EXEC_BATCH_ID_COLUMN,
    EvolveOperation,
    EvolveRequest,
    EvolveResult,
    LiveSchema,
    RenameColumnOp,
    RenameSchemaOp,
    RenameTableOp,
    TableSchema,
    TransferTableOp,
)
from .render_ddl import map_type, qualify_table, render_create_table
from .schema_state import (
    append_evolution_log,
    get_live_schema,
    save_live_schema,
    sync_sequences_after_operations,
)
from .sources import get_source

logger = logging.getLogger("sydafa.schema_engine")


class SchemaEvolutionError(ValueError):
    def __init__(self, step_index: int, op_name: str, message: str):
        super().__init__(f"Операция #{step_index} ('{op_name}') завершилась с ошибкой: {message}")
        self.step_index = step_index
        self.op_name = op_name
        self.message = message


def _table_key(schema: str, table: str) -> str:
    return f"{schema}.{table}"


def _upd_table_name(table_name: str) -> str:
    return f"_upd_{table_name}"


def _del_table_name(table_name: str) -> str:
    return f"_del_{table_name}"


def _make_audit_table_schema(main_table: TableSchema, audit_name: str) -> TableSchema:
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


def _recreate_audit_tables_ddl(
    table_obj: TableSchema,
    dialect: str,
    live: LiveSchema,
) -> list[str]:
    ddl: list[str] = []
    upd_name = _upd_table_name(table_obj.name)
    del_name = _del_table_name(table_obj.name)
    cascade = " CASCADE CONSTRAINTS" if dialect == "oracle" else ""
    ddl.append(f"DROP TABLE {qualify_table(table_obj.schema_, upd_name, dialect)}{cascade}")
    ddl.append(f"DROP TABLE {qualify_table(table_obj.schema_, del_name, dialect)}{cascade}")

    upd_schema = _make_audit_table_schema(table_obj, upd_name)
    del_schema = _make_audit_table_schema(table_obj, del_name)
    ddl.append(render_create_table(upd_schema, dialect))
    ddl.append(render_create_table(del_schema, dialect))

    live.tables[_table_key(table_obj.schema_, upd_name)] = upd_schema
    live.tables[_table_key(table_obj.schema_, del_name)] = del_schema
    return ddl


def _build_ddl_and_update_state(
    op: EvolveOperation,
    dialect: str,
    live: LiveSchema,
    step_index: int,
) -> tuple[list[str], str]:
    """Генерирует DDL-выражения для операции и обновляет объект live (в памяти).

    Возвращает (список DDL-запросов, текстовое описание применённого действия).
    """
    ddl_statements: list[str] = []
    desc = ""

    if isinstance(op, CreateSchemaOp):
        if op.name in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Схема '{op.name}' уже существует")
        if dialect == "mssql":
            ddl_statements.append(
                f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{op.name}') "
                f"EXEC('CREATE SCHEMA [{op.name}] AUTHORIZATION [dbo]')"
            )
        else:  # oracle
            ddl_statements.append(f"CREATE USER {op.name} IDENTIFIED BY sydafa")
            ddl_statements.append(f"GRANT UNLIMITED TABLESPACE TO {op.name}")
            ddl_statements.append(f"GRANT CREATE SESSION, CREATE TABLE TO {op.name}")
        live.schemas.append(op.name)
        desc = f"Создана схема '{op.name}'"

    elif isinstance(op, DropSchemaOp):
        if op.name not in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Схема '{op.name}' не существует")
        # Проверяем, нет ли таблиц в схеме
        tables_in_schema = [t for t in live.tables.values() if t.schema_ == op.name]
        if tables_in_schema:
            raise SchemaEvolutionError(
                step_index,
                op.op,
                f"Невозможно удалить схему '{op.name}', так как в ней присутствуют таблицы: {[t.name for t in tables_in_schema]}",
            )
        if dialect == "mssql":
            ddl_statements.append(f"DROP SCHEMA [{op.name}]")
        else:
            ddl_statements.append(f"DROP USER {op.name} CASCADE")
        live.schemas.remove(op.name)
        desc = f"Удалена схема '{op.name}'"

    elif isinstance(op, RenameSchemaOp):
        if op.from_name not in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Исходная схема '{op.from_name}' не существует")
        if op.to_name in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Целевая схема '{op.to_name}' уже существует")

        tables_to_move = [t for t in live.tables.values() if t.schema_ == op.from_name]
        if dialect == "mssql":
            ddl_statements.append(
                f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{op.to_name}') "
                f"EXEC('CREATE SCHEMA [{op.to_name}] AUTHORIZATION [dbo]')"
            )
            for t in tables_to_move:
                ddl_statements.append(f"ALTER SCHEMA [{op.to_name}] TRANSFER [{op.from_name}].[{t.name}]")
            ddl_statements.append(f"DROP SCHEMA [{op.from_name}]")
        else:
            ddl_statements.append(f"CREATE USER {op.to_name} IDENTIFIED BY sydafa")
            ddl_statements.append(f"GRANT UNLIMITED TABLESPACE TO {op.to_name}")
            ddl_statements.append(f"GRANT CREATE SESSION, CREATE TABLE TO {op.to_name}")
            for t in tables_to_move:
                ddl_statements.append(f"ALTER TABLE {op.from_name}.{t.name} RENAME TO {t.name}")
            ddl_statements.append(f"DROP USER {op.from_name} CASCADE")

        # Обновление схемы в live
        live.schemas.remove(op.from_name)
        live.schemas.append(op.to_name)
        for t in tables_to_move:
            del live.tables[_table_key(op.from_name, t.name)]
            t.schema_ = op.to_name
            live.tables[_table_key(op.to_name, t.name)] = t
        desc = f"Переименована схема '{op.from_name}' -> '{op.to_name}'"

    elif isinstance(op, CreateTableOp):
        if op.schema_ not in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Схема '{op.schema_}' не задекларирована")
        key = _table_key(op.schema_, op.name)
        if key in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{key}' уже существует")

        # Игнорируем пользовательские попытки объявить системные колонки _batch_id или _exec_batch_id
        filtered_cols = [c for c in op.columns if c.name not in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN)]
        filtered_pk = [k for k in op.primary_key if k not in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN)]

        # Валидация первичного ключа
        if filtered_pk:
            col_map = {c.name: c for c in filtered_cols}
            for pk_col in filtered_pk:
                if pk_col not in col_map:
                    raise SchemaEvolutionError(
                        step_index, op.op, f"Колонка первичного ключа '{pk_col}' не найдена в таблице '{key}'"
                    )

            fk_col_names = {c for fk in op.foreign_keys for c in fk.columns}
            has_unique_col = any(
                "uuid" in col_map[c].type.lower()
                or (any(t in col_map[c].type.lower() for t in ("int", "bigint", "smallint", "tinyint")) and c not in fk_col_names)
                or getattr(getattr(col_map[c], "generator", None), "kind", None) in ("sequence", "uuid")
                for c in filtered_pk
            )
            all_are_fk = all(c in fk_col_names for c in filtered_pk)
            if not has_unique_col and not all_are_fk:
                raise SchemaEvolutionError(
                    step_index,
                    op.op,
                    f"Primary key таблицы '{key}' должен включать хотя бы одну уникальную колонку ('sequence' или 'uuid') либо состоять из внешних ключей",
                )

        # Системно внедряем обязательную колонку _batch_id
        filtered_cols.append(ColumnSpec(name=BATCH_ID_COLUMN, type="bigint"))

        t_schema = TableSchema(
            name=op.name,
            schema=op.schema_,
            primary_key=filtered_pk,
            columns=filtered_cols,
            foreign_keys=op.foreign_keys,
        )
        ddl_statements.append(render_create_table(t_schema, dialect))
        live.tables[key] = t_schema

        # Создаем пустые таблицы аудита _upd_ и _del_
        upd_name = _upd_table_name(op.name)
        del_name = _del_table_name(op.name)
        upd_schema = _make_audit_table_schema(t_schema, upd_name)
        del_schema = _make_audit_table_schema(t_schema, del_name)
        ddl_statements.append(render_create_table(upd_schema, dialect))
        ddl_statements.append(render_create_table(del_schema, dialect))
        live.tables[_table_key(op.schema_, upd_name)] = upd_schema
        live.tables[_table_key(op.schema_, del_name)] = del_schema

        desc = f"Создана таблица '{key}' и пустые таблицы аудита '{upd_name}', '{del_name}'"

    elif isinstance(op, DropTableOp):
        key = _table_key(op.schema_, op.name)
        if key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{key}' не найдена")

        cascade = " CASCADE CONSTRAINTS" if dialect == "oracle" else ""
        ddl_statements.append(f"DROP TABLE {qualify_table(op.schema_, op.name, dialect)}{cascade}")
        upd_name = _upd_table_name(op.name)
        del_name = _del_table_name(op.name)
        ddl_statements.append(f"DROP TABLE {qualify_table(op.schema_, upd_name, dialect)}{cascade}")
        ddl_statements.append(f"DROP TABLE {qualify_table(op.schema_, del_name, dialect)}{cascade}")

        live.tables.pop(key, None)
        live.tables.pop(_table_key(op.schema_, upd_name), None)
        live.tables.pop(_table_key(op.schema_, del_name), None)
        desc = f"Удалена таблица '{key}' и таблицы аудита"

    elif isinstance(op, RenameTableOp):
        old_key = _table_key(op.schema_, op.from_name)
        new_key = _table_key(op.schema_, op.to_name)
        if old_key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{old_key}' не найдена")
        if new_key in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{new_key}' уже существует")

        old_upd = _upd_table_name(op.from_name)
        new_upd = _upd_table_name(op.to_name)
        old_del = _del_table_name(op.from_name)
        new_del = _del_table_name(op.to_name)

        if dialect == "mssql":
            ddl_statements.append(f"EXEC sp_rename '[{op.schema_}].[{op.from_name}]', '{op.to_name}'")
            ddl_statements.append(f"EXEC sp_rename '[{op.schema_}].[{old_upd}]', '{new_upd}'")
            ddl_statements.append(f"EXEC sp_rename '[{op.schema_}].[{old_del}]', '{new_del}'")
        else:
            ddl_statements.append(f"ALTER TABLE {op.schema_}.{op.from_name} RENAME TO {op.to_name}")
            ddl_statements.append(f'ALTER TABLE {op.schema_}."{old_upd}" RENAME TO "{new_upd}"')
            ddl_statements.append(f'ALTER TABLE {op.schema_}."{old_del}" RENAME TO "{new_del}"')

        table_obj = live.tables.pop(old_key)
        table_obj.name = op.to_name
        live.tables[new_key] = table_obj

        if _table_key(op.schema_, old_upd) in live.tables:
            upd_obj = live.tables.pop(_table_key(op.schema_, old_upd))
            upd_obj.name = new_upd
            live.tables[_table_key(op.schema_, new_upd)] = upd_obj

        if _table_key(op.schema_, old_del) in live.tables:
            del_obj = live.tables.pop(_table_key(op.schema_, old_del))
            del_obj.name = new_del
            live.tables[_table_key(op.schema_, new_del)] = del_obj

        desc = f"Переименована таблица '{old_key}' -> '{new_key}'"

    elif isinstance(op, TransferTableOp):
        old_key = _table_key(op.from_schema, op.table)
        new_key = _table_key(op.to_schema, op.table)
        if old_key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{old_key}' не найдена")
        if op.to_schema not in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Целевая схема '{op.to_schema}' не существует")
        if new_key in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{new_key}' уже существует в схеме '{op.to_schema}'")

        upd_name = _upd_table_name(op.table)
        del_name = _del_table_name(op.table)

        if dialect == "mssql":
            ddl_statements.append(f"ALTER SCHEMA [{op.to_schema}] TRANSFER [{op.from_schema}].[{op.table}]")
            ddl_statements.append(f"ALTER SCHEMA [{op.to_schema}] TRANSFER [{op.from_schema}].[{upd_name}]")
            ddl_statements.append(f"ALTER SCHEMA [{op.to_schema}] TRANSFER [{op.from_schema}].[{del_name}]")
        else:
            ddl_statements.append(
                f'CREATE TABLE "{op.to_schema}"."{op.table}" AS SELECT * FROM "{op.from_schema}"."{op.table}"'
            )
            ddl_statements.append(f'DROP TABLE "{op.from_schema}"."{op.table}" CASCADE CONSTRAINTS')
            ddl_statements.append(
                f'CREATE TABLE "{op.to_schema}"."{upd_name}" AS SELECT * FROM "{op.from_schema}"."{upd_name}"'
            )
            ddl_statements.append(f'DROP TABLE "{op.from_schema}"."{upd_name}" CASCADE CONSTRAINTS')
            ddl_statements.append(
                f'CREATE TABLE "{op.to_schema}"."{del_name}" AS SELECT * FROM "{op.from_schema}"."{del_name}"'
            )
            ddl_statements.append(f'DROP TABLE "{op.from_schema}"."{del_name}" CASCADE CONSTRAINTS')

        table_obj = live.tables.pop(old_key)
        table_obj.schema_ = op.to_schema
        live.tables[new_key] = table_obj

        if _table_key(op.from_schema, upd_name) in live.tables:
            upd_obj = live.tables.pop(_table_key(op.from_schema, upd_name))
            upd_obj.schema_ = op.to_schema
            live.tables[_table_key(op.to_schema, upd_name)] = upd_obj

        if _table_key(op.from_schema, del_name) in live.tables:
            del_obj = live.tables.pop(_table_key(op.from_schema, del_name))
            del_obj.schema_ = op.to_schema
            live.tables[_table_key(op.to_schema, del_name)] = del_obj

        desc = f"Перенесена таблица '{op.table}' из схемы '{op.from_schema}' в '{op.to_schema}'"

    elif isinstance(op, AddColumnOp):
        if op.column.name in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN):
            raise SchemaEvolutionError(
                step_index, op.op, f"Колонка '{op.column.name}' является системной технической колонкой и не может быть добавлена вручную"
            )
        key = _table_key(op.schema_, op.table)
        if key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{key}' не найдена")
        table_obj = live.tables[key]
        if op.column.name in table_obj.column_names:
            raise SchemaEvolutionError(
                step_index, op.op, f"Колонка '{op.column.name}' уже существует в таблице '{key}'"
            )

        col_type_sql = map_type(op.column.type, dialect)
        if dialect == "mssql":
            ddl_statements.append(f"ALTER TABLE [{op.schema_}].[{op.table}] ADD [{op.column.name}] {col_type_sql} NULL")
        else:
            ddl_statements.append(f"ALTER TABLE {op.schema_}.{op.table} ADD ({op.column.name} {col_type_sql})")

        table_obj.columns.append(op.column)
        ddl_statements.extend(_recreate_audit_tables_ddl(table_obj, dialect, live))
        desc = f"Добавлена колонка '{op.column.name}' ({op.column.type}) в таблицу '{key}', таблицы аудита пересозданы"

    elif isinstance(op, DropColumnOp):
        if op.name in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN):
            raise SchemaEvolutionError(
                step_index, op.op, f"Запрещено удалять системную техническую колонку '{op.name}'"
            )
        key = _table_key(op.schema_, op.table)
        if key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{key}' не найдена")
        table_obj = live.tables[key]
        if op.name not in table_obj.column_names:
            raise SchemaEvolutionError(step_index, op.op, f"Колонка '{op.name}' не найдена в таблице '{key}'")
        if op.name in table_obj.primary_key:
            raise SchemaEvolutionError(step_index, op.op, f"Нельзя удалить колонку '{op.name}', входящую в primary key")

        if dialect == "mssql":
            ddl_statements.append(f"ALTER TABLE [{op.schema_}].[{op.table}] DROP COLUMN [{op.name}]")
        else:
            ddl_statements.append(f"ALTER TABLE {op.schema_}.{op.table} DROP COLUMN {op.name}")

        table_obj.columns = [c for c in table_obj.columns if c.name != op.name]
        ddl_statements.extend(_recreate_audit_tables_ddl(table_obj, dialect, live))
        desc = f"Удалена колонка '{op.name}' из таблицы '{key}', таблицы аудита пересозданы"

    elif isinstance(op, RenameColumnOp):
        if op.from_name in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN) or op.to_name in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN):
            raise SchemaEvolutionError(
                step_index, op.op, "Запрещено переименовывать системную техническую колонку"
            )
        key = _table_key(op.schema_, op.table)
        if key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{key}' не найдена")
        table_obj = live.tables[key]
        if op.from_name not in table_obj.column_names:
            raise SchemaEvolutionError(step_index, op.op, f"Колонка '{op.from_name}' не найдена в таблице '{key}'")
        if op.to_name in table_obj.column_names:
            raise SchemaEvolutionError(step_index, op.op, f"Колонка '{op.to_name}' уже существует в таблице '{key}'")

        if dialect == "mssql":
            ddl_statements.append(
                f"EXEC sp_rename '[{op.schema_}].[{op.table}].[{op.from_name}]', '{op.to_name}', 'COLUMN'"
            )
        else:
            ddl_statements.append(f"ALTER TABLE {op.schema_}.{op.table} RENAME COLUMN {op.from_name} TO {op.to_name}")

        for c in table_obj.columns:
            if c.name == op.from_name:
                c.name = op.to_name
        table_obj.primary_key = [op.to_name if pk == op.from_name else pk for pk in table_obj.primary_key]
        ddl_statements.extend(_recreate_audit_tables_ddl(table_obj, dialect, live))
        desc = f"Переименована колонка '{op.from_name}' -> '{op.to_name}' в таблице '{key}', таблицы аудита пересозданы"

    return ddl_statements, desc


def execute_evolve(source_name: str, request: EvolveRequest) -> EvolveResult:
    """Выполняет пакет операций эволюции в единой транзакции (All-or-Nothing)."""
    cfg = get_source(source_name)
    dialect = cfg.type

    # 1. Загрузка текущего live состояния
    live = get_live_schema(source_name)
    working_live = copy.deepcopy(live)

    all_ddl: list[str] = []
    applied_descriptions: list[str] = []

    # 2. Предварительная валидация и генерация DDL для всех шагов
    for idx, op in enumerate(request.operations):
        statements, desc = _build_ddl_and_update_state(op, dialect, working_live, idx)
        all_ddl.extend(statements)
        applied_descriptions.append(desc)

    # 3. Исполнение в БД в единой транзакции
    from .db_connectors import get_connection
    with get_connection(source_name, autocommit=False) as conn:
        cur = conn.cursor()
        for idx, sql in enumerate(all_ddl):
            try:
                cur.execute(sql)
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                msg = f"Ошибка выполнения DDL: '{sql}'. Детали: {e}"
                logger.error("Транзакция эволюции схемы источника '%s' откатана: %s", source_name, msg)
                raise SchemaEvolutionError(idx, "ddl_execution", msg) from e

        # Фиксация изменений в БД
        conn.commit()

        # Oracle: перевыпуск грантов читателю
        if dialect == "oracle":
            try:
                cur.execute(f"GRANT SELECT ANY TABLE TO {cfg.reader}")
                conn.commit()
            except Exception:
                pass

    # 4. Обновление состояния и журнала
    save_live_schema(source_name, working_live)
    sync_sequences_after_operations(source_name, request.operations)
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source_name,
        "operations_count": len(request.operations),
        "applied": applied_descriptions,
    }
    append_evolution_log(source_name, log_entry)

    logger.info("Эволюция схемы источника '%s' успешно завершена: %d операций.", source_name, len(request.operations))
    return EvolveResult(
        status="ok",
        executed=len(request.operations),
        applied=applied_descriptions,
    )
