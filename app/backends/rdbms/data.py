"""SQL-чтения реляционных данных: FK-родители, накопительные балансы, инварианты.

SQL строится через диалект движка; соединением владеет вызывающий слой
(одна рабочая session на сценарий). Известное сохраняемое поведение:
сбой предзагрузки балансов подавляется вызывающим кодом (backlog №12).
"""
from __future__ import annotations

import random
from typing import Any

from ...models import CumulativeBalanceInvariant, TableSchema


def pick_existing_pk(conn, dialect, qualified: str, pk_cols: list[str]) -> dict[str, Any] | None:
    """Случайный существующий PK родительской таблицы или None, если родитель пуст."""
    cols = ", ".join(dialect.quote_identifier(c) for c in pk_cols)
    cur = conn.cursor()
    cur.execute(f"SELECT {cols} FROM {qualified}")
    rows = cur.fetchall()
    if not rows:
        return None
    row = random.choice(rows)
    return dict(zip(pk_cols, row))


def load_existing_balances(
    conn, dialect, qualified: str, inv: CumulativeBalanceInvariant, balances: dict
) -> None:
    """Заполняет переданный словарь накопленных балансов построчно.

    Инкрементальное наполнение — часть сохраняемого поведения: при ошибке
    чтения/преобразования поздней строки уже заполненная часть остаётся
    (подавление исключения — на вызывающем слое, backlog №12).
    """
    amt_col = dialect.quote_identifier(inv.amount_column)
    cur = conn.cursor()
    if inv.partition_by:
        part_cols = ", ".join(dialect.quote_identifier(c) for c in inv.partition_by)
        sql = f"SELECT {part_cols}, SUM({amt_col}) FROM {qualified} GROUP BY {part_cols}"
        cur.execute(sql)
        for r in cur.fetchall():
            part_vals = tuple(r[: len(inv.partition_by)])
            sum_val = float(r[-1]) if r[-1] is not None else 0.0
            balances[part_vals] = sum_val
    else:
        sql = f"SELECT SUM({amt_col}) FROM {qualified}"
        cur.execute(sql)
        rows = cur.fetchall()
        if rows and rows[0] and rows[0][0] is not None:
            balances[()] = float(rows[0][0])
        else:
            balances[()] = 0.0


def fetch_invariant_violations(
    conn, dialect, qualified: str, inv: CumulativeBalanceInvariant
) -> list[Any]:
    """Строки-нарушения min_balance (HAVING); пустой результат — нарушений нет."""
    amt_col = dialect.quote_identifier(inv.amount_column)
    cur = conn.cursor()
    if inv.partition_by:
        part_cols = ", ".join(dialect.quote_identifier(c) for c in inv.partition_by)
        sql = f"SELECT {part_cols}, SUM({amt_col}) FROM {qualified} GROUP BY {part_cols} HAVING SUM({amt_col}) < {inv.min_balance}"
    else:
        sql = f"SELECT SUM({amt_col}) FROM {qualified} HAVING SUM({amt_col}) < {inv.min_balance}"
    cur.execute(sql)
    return cur.fetchall()


def cascade_dependent_pk_values(table: TableSchema, fk, pk_row) -> list[Any]:
    """Значения FK-колонок зависимой таблицы для строки PK родителя (порядок ref_columns)."""
    ref_idx = {c: i for i, c in enumerate(table.primary_key)}
    return [pk_row[ref_idx[rc]] for rc in fk.ref_columns]
