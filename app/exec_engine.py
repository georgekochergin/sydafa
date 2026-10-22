"""Движок транзакционного выполнения операций DDL + DML (All-or-Nothing) для источников."""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from .db_connectors import get_connection
from .generators import (
    GeneratorError,
    PredicateError,
    append_rows_conn,
    delete_rows_conn,
    truncate_table_conn,
    update_rows_conn,
)
from .models import (
    AppendOp,
    DeleteOp,
    ExecOperation,
    ExecResult,
    LiveSchema,
    TruncateOp,
    UpdateOp,
)
from .schema_engine import SchemaEvolutionError, _build_ddl_and_update_state
from .schema_state import (
    append_evolution_log,
    get_live_schema,
    get_next_batch_id,
    save_live_schema,
    sync_sequences_after_operations,
)
from .sources import get_source

logger = logging.getLogger("sydafa.exec_engine")


class ExecError(ValueError):
    def __init__(self, step_index: int, op_name: str, message: str, is_conflict: bool = False):
        super().__init__(f"Операция #{step_index} ('{op_name}') завершилась с ошибкой: {message}")
        self.step_index = step_index
        self.op_name = op_name
        self.message = message
        self.is_conflict = is_conflict


def execute_source_operations(source_name: str, operations: list[ExecOperation]) -> ExecResult:
    """Выполняет батч операций DDL и DML в единой транзакции для указанного источника.

    Гарантия All-or-Nothing: при сбое любой операции транзакция в СУБД откатывается,
    а состояние LiveSchema в памяти восстанавливается.
    """
    cfg = get_source(source_name)
    dialect = cfg.type
    initial_live = get_live_schema(source_name)
    live: LiveSchema = copy.deepcopy(initial_live)

    current_batch_id = get_next_batch_id(source_name)

    applied_records: list[dict[str, Any]] = []

    with get_connection(source_name) as conn:
        try:
            for step_idx, op in enumerate(operations):
                op_name = getattr(op, "op", "unknown")
                if isinstance(op, TruncateOp):
                    truncate_table_conn(conn, dialect, live, op.schema_, op.table, op.cascade, current_batch_id)
                    applied_records.append({
                        "step": step_idx,
                        "op": "truncate",
                        "entity_fqn": op.entity_fqn,
                    })

                elif isinstance(op, AppendOp):
                    inserted = append_rows_conn(
                        conn, source_name, dialect, live, op.schema_, op.table, op.rows, current_batch_id
                    )
                    applied_records.append({
                        "step": step_idx,
                        "op": "append",
                        "entity_fqn": op.entity_fqn,
                        "inserted": inserted,
                    })

                elif isinstance(op, UpdateOp):
                    updated = update_rows_conn(
                        conn, dialect, live, op.schema_, op.table, op.predicate, op.set, current_batch_id, limit=op.limit
                    )
                    # Строгий Invariant Guard (только если для таблицы включен validate_existing=True)
                    from . import rules_state
                    entity_fqn = op.entity_fqn
                    profile = rules_state.get_table_rules(source_name, entity_fqn)
                    if profile and any(getattr(inv, "validate_existing", False) for inv in profile.invariants):
                        try:
                            rules_state.validate_table_invariants_on_data(source_name, entity_fqn, profile, conn=conn)
                        except ValueError as err:
                            raise ExecError(step_idx, op_name, str(err)) from err

                    applied_records.append({
                        "step": step_idx,
                        "op": "update",
                        "entity_fqn": op.entity_fqn,
                        "updated": updated,
                    })

                elif isinstance(op, DeleteOp):
                    deleted = delete_rows_conn(
                        conn, dialect, live, op.schema_, op.table, op.predicate, op.cascade, current_batch_id, limit=op.limit
                    )
                    # Строгий Invariant Guard (только если для таблицы включен validate_existing=True)
                    from . import rules_state
                    entity_fqn = op.entity_fqn
                    profile = rules_state.get_table_rules(source_name, entity_fqn)
                    if profile and any(getattr(inv, "validate_existing", False) for inv in profile.invariants):
                        try:
                            rules_state.validate_table_invariants_on_data(source_name, entity_fqn, profile, conn=conn)
                        except ValueError as err:
                            raise ExecError(step_idx, op_name, str(err)) from err

                    applied_records.append({
                        "step": step_idx,
                        "op": "delete",
                        "entity_fqn": op.entity_fqn,
                        "deleted": deleted,
                    })

                else:
                    # DDL операция
                    statements, desc = _build_ddl_and_update_state(op, dialect, live, step_idx)
                    cur = conn.cursor()
                    for stmt in statements:
                        cur.execute(stmt)
                    record = {
                        "step": step_idx,
                        "op": op_name,
                        "description": desc,
                    }
                    if hasattr(op, "entity_fqn"):
                        record["entity_fqn"] = op.entity_fqn
                    applied_records.append(record)

            # Фиксация транзакции в БД
            conn.commit()

        except GeneratorError as e:
            conn.rollback()
            # Ошибки FK каскада — это HTTP 409 Conflict
            is_conflict = "ссыла" in str(e) or "cascade" in str(e)
            raise ExecError(step_idx, op_name, str(e), is_conflict=is_conflict) from e

        except (PredicateError, SchemaEvolutionError) as e:
            conn.rollback()
            raise ExecError(
                getattr(e, "step_index", step_idx),
                getattr(e, "op_name", op_name),
                getattr(e, "message", str(e)),
            ) from e

        except Exception as e:
            conn.rollback()
            msg = f"Ошибка выполнения SQL/DML: {e}"
            logger.exception("Ошибка шага #%d (%s): %s", step_idx, op_name, e)
            raise ExecError(step_idx, op_name, msg) from e

    # Сохраняем обновленную схему
    save_live_schema(source_name, live)
    sync_sequences_after_operations(source_name, operations)

    # Логируем в историю эволюции
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source_name,
        "operations_count": len(operations),
        "applied": applied_records,
    }
    append_evolution_log(source_name, log_entry)

    return ExecResult(
        status="ok",
        executed=len(operations),
        applied=applied_records,
    )
