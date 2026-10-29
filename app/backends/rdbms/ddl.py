"""Общая валидация DDL-операций и обновление LiveSchema; SQL строит диалект.

Перенос `schema_engine._build_ddl_and_update_state` без vendor-веток:
проверки ссылок, PK/FK-инварианты, аудит-таблицы и переходы live-модели —
здесь; конкретный SQL — у диалекта движка.
"""
from __future__ import annotations

import logging

from ...models import (
    BATCH_ID_COLUMN,
    EXEC_BATCH_ID_COLUMN,
    AddColumnOp,
    ColumnSpec,
    CreateSchemaOp,
    CreateTableOp,
    DropColumnOp,
    DropSchemaOp,
    DropTableOp,
    EvolveOperation,
    LiveSchema,
    RenameColumnOp,
    RenameSchemaOp,
    RenameTableOp,
    TableSchema,
    TransferTableOp,
)
from .schema import (
    del_table_name,
    make_audit_table_schema,
    recreate_audit_tables_ddl,
    table_key,
    upd_table_name,
)

logger = logging.getLogger("sydafa.backends.rdbms.ddl")


class SchemaEvolutionError(ValueError):
    def __init__(self, step_index: int, op_name: str, message: str):
        super().__init__(f"Операция #{step_index} ('{op_name}') завершилась с ошибкой: {message}")
        self.step_index = step_index
        self.op_name = op_name
        self.message = message


def build_ddl_and_update_state(
    op: EvolveOperation,
    dialect,
    live: LiveSchema,
    step_index: int,
) -> tuple[list[str], str]:
    """Генерирует DDL-выражения операции через диалект и обновляет live (в памяти).

    Возвращает (список DDL-запросов, текстовое описание применённого действия).
    """
    ddl_statements: list[str] = []
    desc = ""

    if isinstance(op, CreateSchemaOp):
        if op.name in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Схема '{op.name}' уже существует")
        ddl_statements.extend(dialect.create_schema(op.name))
        live.schemas.append(op.name)
        desc = f"Создана схема '{op.name}'"

    elif isinstance(op, DropSchemaOp):
        if op.name not in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Схема '{op.name}' не существует")
        tables_in_schema = [t for t in live.tables.values() if t.schema_ == op.name]
        if tables_in_schema:
            raise SchemaEvolutionError(
                step_index,
                op.op,
                f"Невозможно удалить схему '{op.name}', так как в ней присутствуют таблицы: {[t.name for t in tables_in_schema]}",
            )
        ddl_statements.extend(dialect.drop_schema(op.name))
        live.schemas.remove(op.name)
        desc = f"Удалена схема '{op.name}'"

    elif isinstance(op, RenameSchemaOp):
        if op.from_name not in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Исходная схема '{op.from_name}' не существует")
        if op.to_name in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Целевая схема '{op.to_name}' уже существует")

        tables_to_move = [t for t in live.tables.values() if t.schema_ == op.from_name]
        ddl_statements.extend(
            dialect.rename_schema(op.from_name, op.to_name, [t.name for t in tables_to_move])
        )

        live.schemas.remove(op.from_name)
        live.schemas.append(op.to_name)
        for t in tables_to_move:
            del live.tables[table_key(op.from_name, t.name)]
            t.schema_ = op.to_name
            live.tables[table_key(op.to_name, t.name)] = t
        desc = f"Переименована схема '{op.from_name}' -> '{op.to_name}'"

    elif isinstance(op, CreateTableOp):
        if op.schema_ not in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Схема '{op.schema_}' не задекларирована")
        key = table_key(op.schema_, op.name)
        if key in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{key}' уже существует")

        # Игнорируем пользовательские попытки объявить системные колонки _batch_id или _exec_batch_id
        filtered_cols = [c for c in op.columns if c.name not in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN)]
        filtered_pk = [k for k in op.primary_key if k not in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN)]

        # Резолвинг и принудительное наследование типов колонок из родительских таблиц Foreign Keys
        fk_map: dict[str, tuple[str, str]] = {}
        for fk in op.foreign_keys:
            ref_fqn = fk.ref_entity_fqn
            if ref_fqn not in live.tables:
                raise SchemaEvolutionError(
                    step_index, op.op, f"Родительская таблица '{ref_fqn}' для Foreign Key не найдена в схеме"
                )
            parent_table = live.tables[ref_fqn]
            parent_col_map = {c.name: c for c in parent_table.columns}
            for idx, col_name in enumerate(fk.columns):
                ref_col_name = fk.ref_columns[idx]
                if ref_col_name not in parent_col_map:
                    raise SchemaEvolutionError(
                        step_index, op.op, f"Колонка '{ref_col_name}' не найдена в родительской таблице '{ref_fqn}'"
                    )
                fk_map[col_name] = (ref_fqn, parent_col_map[ref_col_name].type)

        for c in filtered_cols:
            if c.name in fk_map:
                # Принудительно выставляем тип родительской колонки
                _, parent_type = fk_map[c.name]
                c.type = parent_type
            else:
                if not c.type:
                    raise SchemaEvolutionError(
                        step_index, op.op, f"Колонка '{c.name}' таблицы '{key}' не входит в foreign_keys и обязана иметь 'type'"
                    )

        # Валидация первичного ключа
        if filtered_pk:
            col_map = {c.name: c for c in filtered_cols}
            for pk_col in filtered_pk:
                if pk_col not in col_map:
                    raise SchemaEvolutionError(
                        step_index, op.op, f"Колонка первичного ключа '{pk_col}' не найдена в таблице '{key}'"
                    )

            fk_col_names = set(fk_map.keys())
            has_unique_col = any(
                getattr(getattr(col_map[c], "generator", None), "kind", None) in ("sequence", "uuid")
                for c in filtered_pk
            )
            all_are_fk = all(c in fk_col_names for c in filtered_pk)
            if not has_unique_col and not all_are_fk:
                raise SchemaEvolutionError(
                    step_index,
                    op.op,
                    f"Primary key таблицы '{key}' должен включать хотя бы одну уникальную колонку с явным генератором ('sequence' или 'uuid') либо состоять из внешних ключей",
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
        ddl_statements.append(dialect.create_table(t_schema))
        live.tables[key] = t_schema

        # Создаем пустые таблицы аудита _upd_ и _del_
        upd_name = upd_table_name(op.name)
        del_name = del_table_name(op.name)
        upd_schema = make_audit_table_schema(t_schema, upd_name)
        del_schema = make_audit_table_schema(t_schema, del_name)
        ddl_statements.append(dialect.create_table(upd_schema))
        ddl_statements.append(dialect.create_table(del_schema))
        live.tables[table_key(op.schema_, upd_name)] = upd_schema
        live.tables[table_key(op.schema_, del_name)] = del_schema

        desc = f"Создана таблица '{key}' и пустые таблицы аудита '{upd_name}', '{del_name}'"

    elif isinstance(op, DropTableOp):
        key = table_key(op.schema_, op.name)
        if key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{key}' не найдена")

        ddl_statements.append(dialect.drop_table(op.schema_, op.name))
        ddl_statements.append(dialect.drop_table(op.schema_, upd_table_name(op.name)))
        ddl_statements.append(dialect.drop_table(op.schema_, del_table_name(op.name)))

        live.tables.pop(key, None)
        live.tables.pop(table_key(op.schema_, upd_table_name(op.name)), None)
        live.tables.pop(table_key(op.schema_, del_table_name(op.name)), None)
        desc = f"Удалена таблица '{key}' и таблицы аудита"

    elif isinstance(op, RenameTableOp):
        old_key = table_key(op.schema_, op.from_name)
        new_key = table_key(op.schema_, op.to_name)
        if old_key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{old_key}' не найдена")
        if new_key in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{new_key}' уже существует")

        old_upd = upd_table_name(op.from_name)
        new_upd = upd_table_name(op.to_name)
        old_del = del_table_name(op.from_name)
        new_del = del_table_name(op.to_name)

        ddl_statements.append(dialect.rename_table(op.schema_, op.from_name, op.to_name))
        ddl_statements.append(dialect.rename_table(op.schema_, old_upd, new_upd))
        ddl_statements.append(dialect.rename_table(op.schema_, old_del, new_del))

        table_obj = live.tables.pop(old_key)
        table_obj.name = op.to_name
        live.tables[new_key] = table_obj

        if table_key(op.schema_, old_upd) in live.tables:
            upd_obj = live.tables.pop(table_key(op.schema_, old_upd))
            upd_obj.name = new_upd
            live.tables[table_key(op.schema_, new_upd)] = upd_obj

        if table_key(op.schema_, old_del) in live.tables:
            del_obj = live.tables.pop(table_key(op.schema_, old_del))
            del_obj.name = new_del
            live.tables[table_key(op.schema_, new_del)] = del_obj

        desc = f"Переименована таблица '{old_key}' -> '{new_key}'"

    elif isinstance(op, TransferTableOp):
        old_key = table_key(op.from_schema, op.table)
        new_key = table_key(op.to_schema, op.table)
        if old_key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{old_key}' не найдена")
        if op.to_schema not in live.schemas:
            raise SchemaEvolutionError(step_index, op.op, f"Целевая схема '{op.to_schema}' не существует")
        if new_key in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{new_key}' уже существует в схеме '{op.to_schema}'")

        upd_name = upd_table_name(op.table)
        del_name = del_table_name(op.table)

        for name in (op.table, upd_name, del_name):
            ddl_statements.extend(dialect.transfer_table(op.from_schema, op.to_schema, name))

        table_obj = live.tables.pop(old_key)
        table_obj.schema_ = op.to_schema
        live.tables[new_key] = table_obj

        if table_key(op.from_schema, upd_name) in live.tables:
            upd_obj = live.tables.pop(table_key(op.from_schema, upd_name))
            upd_obj.schema_ = op.to_schema
            live.tables[table_key(op.to_schema, upd_name)] = upd_obj

        if table_key(op.from_schema, del_name) in live.tables:
            del_obj = live.tables.pop(table_key(op.from_schema, del_name))
            del_obj.schema_ = op.to_schema
            live.tables[table_key(op.to_schema, del_name)] = del_obj

        desc = f"Перенесена таблица '{op.table}' из схемы '{op.from_schema}' в '{op.to_schema}'"

    elif isinstance(op, AddColumnOp):
        if op.column.name in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN):
            raise SchemaEvolutionError(
                step_index, op.op, f"Колонка '{op.column.name}' является системной технической колонкой и не может быть добавлена вручную"
            )
        key = table_key(op.schema_, op.table)
        if key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{key}' не найдена")
        table_obj = live.tables[key]
        if op.column.name in table_obj.column_names:
            raise SchemaEvolutionError(
                step_index, op.op, f"Колонка '{op.column.name}' уже существует в таблице '{key}'"
            )

        ddl_statements.append(dialect.add_column(op.schema_, op.table, op.column))
        table_obj.columns.append(op.column)
        ddl_statements.extend(recreate_audit_tables_ddl(dialect, table_obj, live))
        desc = f"Добавлена колонка '{op.column.name}' ({op.column.type}) в таблицу '{key}', таблицы аудита пересозданы"

    elif isinstance(op, DropColumnOp):
        if op.name in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN):
            raise SchemaEvolutionError(
                step_index, op.op, f"Запрещено удалять системную техническую колонку '{op.name}'"
            )
        key = table_key(op.schema_, op.table)
        if key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{key}' не найдена")
        table_obj = live.tables[key]
        if op.name not in table_obj.column_names:
            raise SchemaEvolutionError(step_index, op.op, f"Колонка '{op.name}' не найдена в таблице '{key}'")
        if op.name in table_obj.primary_key:
            raise SchemaEvolutionError(step_index, op.op, f"Нельзя удалить колонку '{op.name}', входящую в primary key")

        ddl_statements.append(dialect.drop_column(op.schema_, op.table, op.name))
        table_obj.columns = [c for c in table_obj.columns if c.name != op.name]
        ddl_statements.extend(recreate_audit_tables_ddl(dialect, table_obj, live))
        desc = f"Удалена колонка '{op.name}' из таблицы '{key}', таблицы аудита пересозданы"

    elif isinstance(op, RenameColumnOp):
        if op.from_name in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN) or op.to_name in (BATCH_ID_COLUMN, EXEC_BATCH_ID_COLUMN):
            raise SchemaEvolutionError(
                step_index, op.op, "Запрещено переименовывать системную техническую колонку"
            )
        key = table_key(op.schema_, op.table)
        if key not in live.tables:
            raise SchemaEvolutionError(step_index, op.op, f"Таблица '{key}' не найдена")
        table_obj = live.tables[key]
        if op.from_name not in table_obj.column_names:
            raise SchemaEvolutionError(step_index, op.op, f"Колонка '{op.from_name}' не найдена в таблице '{key}'")
        if op.to_name in table_obj.column_names:
            raise SchemaEvolutionError(step_index, op.op, f"Колонка '{op.to_name}' уже существует в таблице '{key}'")

        ddl_statements.append(dialect.rename_column(op.schema_, op.table, op.from_name, op.to_name))

        for c in table_obj.columns:
            if c.name == op.from_name:
                c.name = op.to_name
        table_obj.primary_key = [op.to_name if pk == op.from_name else pk for pk in table_obj.primary_key]
        ddl_statements.extend(recreate_audit_tables_ddl(dialect, table_obj, live))
        desc = f"Переименована колонка '{op.from_name}' -> '{op.to_name}' в таблице '{key}', таблицы аудита пересозданы"

    return ddl_statements, desc
