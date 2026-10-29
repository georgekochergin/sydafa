"""RDBMS-инициализатор: декларативная схема sources.json -> батч DDL-операций.

Общий init-маркер и оркестрация первичного наката остаются в `init_apply`;
здесь — только трансляция декларативного описания в операции (топологическая
сортировка таблиц по FK, наследование генераторов из rules).
"""
from __future__ import annotations

from typing import Any

from ...models import ColumnSpec, CreateSchemaOp, CreateTableOp, ExecOperation, ForeignKey, validate_entity_fqn


def compile_schema_decl_to_operations(
    schema_data: dict[str, Any], rules_data: dict[str, Any] | None = None
) -> list[ExecOperation]:
    """Транслирует декларативное описание schema из sources.json в упорядоченный батч DDL операций."""
    tables_dict = schema_data.get("tables", {})
    if not isinstance(tables_dict, dict) or not tables_dict:
        return []

    schemas: list[str] = []
    parsed_tables: list[tuple[str, str, dict[str, Any]]] = []
    for fqn, t_spec in tables_dict.items():
        schema_name, table_name = validate_entity_fqn(fqn)
        if schema_name not in schemas:
            schemas.append(schema_name)
        parsed_tables.append((schema_name, table_name, t_spec if isinstance(t_spec, dict) else {}))

    operations: list[ExecOperation] = [CreateSchemaOp(name=s) for s in schemas]

    # Топологическая сортировка таблиц по Foreign Keys (родительские таблицы перед дочерними)
    deps: dict[str, set[str]] = {}
    table_map: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for s_name, t_name, t_spec in parsed_tables:
        fqn = f"{s_name}.{t_name}"
        table_map[fqn] = (s_name, t_name, t_spec)
        fks_raw = t_spec.get("foreign_keys", [])
        t_deps: set[str] = set()
        for fk in fks_raw:
            ref_fqn = fk.get("ref_entity_fqn") if isinstance(fk, dict) else getattr(fk, "ref_entity_fqn", None)
            if ref_fqn and ref_fqn != fqn and ref_fqn in tables_dict:
                t_deps.add(ref_fqn)
        deps[fqn] = t_deps

    remaining = set(table_map.keys())
    sorted_fqns: list[str] = []
    while remaining:
        ready = [fqn for fqn in remaining if deps[fqn].issubset(set(sorted_fqns))]
        if not ready:
            sorted_fqns.extend(sorted(remaining))
            break
        for fqn in ready:
            sorted_fqns.append(fqn)
            remaining.remove(fqn)

    for fqn in sorted_fqns:
        s_name, t_name, t_spec = table_map[fqn]
        pk = t_spec.get("primary_key", [])
        raw_cols = t_spec.get("columns", {})
        t_rules = (
            rules_data.get(fqn, {}).get("columns", {})
            if isinstance(rules_data, dict) and isinstance(rules_data.get(fqn), dict)
            else {}
        )
        col_specs: list[ColumnSpec] = []
        if isinstance(raw_cols, dict):
            for col_name, col_info in raw_cols.items():
                if isinstance(col_info, dict):
                    col_type = col_info.get("type")
                    gen = col_info.get("generator") or t_rules.get(col_name)
                elif hasattr(col_info, "type"):
                    col_type = getattr(col_info, "type", None)
                    gen = getattr(col_info, "generator", None) or t_rules.get(col_name)
                else:
                    col_type = None
                    gen = t_rules.get(col_name)
                col_specs.append(ColumnSpec(name=col_name, type=col_type, generator=gen))
        elif isinstance(raw_cols, list):
            for c in raw_cols:
                if isinstance(c, dict):
                    c_name = c["name"]
                    c_type = c["type"]
                    gen = c.get("generator") or t_rules.get(c_name)
                    col_specs.append(ColumnSpec(name=c_name, type=c_type, generator=gen))
                elif hasattr(c, "name") and hasattr(c, "type"):
                    gen = getattr(c, "generator", None) or t_rules.get(c.name)
                    col_specs.append(ColumnSpec(name=c.name, type=c.type, generator=gen))

        fks_list: list[ForeignKey] = []
        for fk in t_spec.get("foreign_keys", []):
            if isinstance(fk, dict):
                fks_list.append(ForeignKey(**fk))
            elif isinstance(fk, ForeignKey):
                fks_list.append(fk)

        operations.append(
            CreateTableOp(
                entity_fqn=f"{s_name}.{t_name}",
                primary_key=pk,
                columns=col_specs,
                foreign_keys=fks_list,
            )
        )

    return operations
