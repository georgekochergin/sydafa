"""Генерация диалектного DDL (MSSQL | Oracle) по моделям TableSchema."""
from __future__ import annotations

import re
from .models import TableSchema

_MSSQL_TYPES = {
    "bigint": "BIGINT",
    "string": "NVARCHAR(400)",
    "date": "DATE",
    "timestamp": "DATETIME2",
}

_ORACLE_TYPES = {
    "bigint": "NUMBER(19)",
    "string": "VARCHAR2(400)",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
}

_DECIMAL_RE = re.compile(r"^decimal\((\d+),\s*(\d+)\)$")


def map_type(logical_type: str, dialect: str) -> str:
    m = _DECIMAL_RE.match(logical_type)
    if m:
        precision, scale = m.groups()
        if dialect == "mssql":
            return f"DECIMAL({precision},{scale})"
        return f"NUMBER({precision},{scale})"

    table = _MSSQL_TYPES if dialect == "mssql" else _ORACLE_TYPES
    try:
        return table[logical_type]
    except KeyError as exc:
        raise ValueError(
            f"Нет правила маппинга логического типа '{logical_type}' для диалекта '{dialect}'"
        ) from exc


def quote_ident(ident: str, dialect: str) -> str:
    """Экранирует имя колонки или таблицы в зависимости от диалекта СУБД."""
    if dialect == "mssql":
        return f"[{ident}]"
    if ident.startswith("_"):
        return f'"{ident}"'
    return ident


def qualify_table(schema: str, table: str, dialect: str) -> str:
    """Формирует полное квалифицированное имя таблицы schema.table с учетом диалекта."""
    if dialect == "mssql":
        return f"[{schema}].[{table}]"
    if table.startswith("_"):
        return f'{schema}."{table}"'
    return f"{schema}.{table}"


def render_create_table(table: TableSchema, dialect: str) -> str:
    """Генерирует CREATE TABLE для таблицы с учётом её схемы и диалекта СУБД."""
    qualified = qualify_table(table.schema_, table.name, dialect)
    if dialect == "mssql":
        col_lines = [f"  [{c.name}] {map_type(c.type, dialect)}" for c in table.columns]
        if table.primary_key:
            pk_cols = ", ".join(f"[{c}]" for c in table.primary_key)
            col_lines.append(f"  CONSTRAINT [pk_{table.schema_}_{table.name}] PRIMARY KEY ({pk_cols})")
        for fk in table.foreign_keys:
            cols = ", ".join(f"[{c}]" for c in fk.columns)
            ref_schema = fk.ref_schema or table.schema_
            ref_cols = ", ".join(f"[{c}]" for c in fk.ref_columns)
            fk_name = f"fk_{table.schema_}_{table.name}_{'_'.join(fk.columns)}"
            col_lines.append(
                f"  CONSTRAINT [{fk_name}] FOREIGN KEY ({cols}) REFERENCES [{ref_schema}].[{fk.ref_table}] ({ref_cols})"
            )
    else:  # oracle
        col_lines = [f"  {quote_ident(c.name, dialect)} {map_type(c.type, dialect)}" for c in table.columns]
        if table.primary_key:
            pk_cols = ", ".join(quote_ident(c, dialect) for c in table.primary_key)
            col_lines.append(f"  CONSTRAINT pk_{table.schema_}_{table.name} PRIMARY KEY ({pk_cols})")
        for fk in table.foreign_keys:
            cols = ", ".join(quote_ident(c, dialect) for c in fk.columns)
            ref_schema = fk.ref_schema or table.schema_
            ref_cols = ", ".join(quote_ident(c, dialect) for c in fk.ref_columns)
            fk_name = f"fk_{table.schema_}_{table.name}_{'_'.join(fk.columns)}"[:30]
            col_lines.append(
                f"  CONSTRAINT {fk_name} FOREIGN KEY ({cols}) REFERENCES {ref_schema}.{fk.ref_table} ({ref_cols})"
            )

    body = ",\n".join(col_lines)
    return f"CREATE TABLE {qualified} (\n{body}\n)"
