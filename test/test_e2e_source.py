"""Сквозное e2e тестирование API sydafa для конкретного источника СУБД."""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PASS = 0
FAIL = 0


def check(name: str, actual: any, expected: any):
    global PASS, FAIL
    ok = str(actual) == str(expected)
    if ok:
        print(f"PASS  {name}")
        PASS += 1
    else:
        print(f"FAIL  {name} (got={actual!r}, want={expected!r})")
        FAIL += 1


def http(method: str, url: str, body=None, headers=None, timeout: int = 300):
    data = None
    if body is not None:
        if isinstance(body, (bytes, str)):
            data = body.encode() if isinstance(body, str) else body
        else:
            data = json.dumps(body).encode()
    h = dict(headers or {})
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                parsed = json.loads(raw) if raw else None
            except Exception:
                parsed = raw
            return r.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw
        return e.code, parsed
    except Exception as e:
        return 0, str(e)


def wait_ready(base_url: str, source: str, timeout: int = 180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, parsed = http("GET", f"{base_url}/health")
        if code == 200 and isinstance(parsed, dict) and source in parsed.get("sources", []):
            return True
        time.sleep(3)
    return False


def run_source_e2e(base_url: str, source: str):
    print(f"\n=== Начало e2e-тестирования источника '{source}' ===")

    # 1. Готовность
    ok = wait_ready(base_url, source)
    check(f"{source}: healthcheck ready", ok, True)
    if not ok:
        return

    # 2. Интроспекция источника (Safe GET)
    code, meta = http("GET", f"{base_url}/sources/{source}")
    check(f"{source}: GET /sources/{source}", code, 200)
    check(f"{source}: database name is sydafa", meta.get("database"), "sydafa")
    check(f"{source}: reader username is sydafa", meta.get("reader"), "sydafa")

    # 2.1. Жизненный цикл (POST /activate с капингом TTL и проверка connection)
    code, act = http("POST", f"{base_url}/sources/{source}/activate?ttl_minutes=45")
    check(f"{source}: POST /activate", code, 200)
    check(f"{source}: activate state is running", act.get("state"), "running")
    check(f"{source}: TTL capped to 30 min", act.get("ttl_minutes"), 30)
    check(f"{source}: activated_until ends with Z (UTC)", str(act.get("activated_until")).endswith("Z"), True)
    conn = act.get("connection", {})
    check(f"{source}: connection has jdbc_url", "jdbc_url" in conn, True)
    check(f"{source}: parameters omitted from connection", "parameters" not in conn, True)

    # 3. DDL: Создание схемы и связанных таблиц с FK через POST /exec
    schema_name = "e2e_demo"
    init_ops = [
        {"op": "create_schema", "name": schema_name},
        {
            "op": "create_table",
            "schema": schema_name,
            "name": "categories",
            "primary_key": ["cat_id"],
            "columns": [
                {"name": "cat_id", "type": "bigint", "generator": {"kind": "sequence"}},
                {"name": "cat_name", "type": "string", "generator": {"kind": "faker", "provider": "word"}},
            ],
            "foreign_keys": [],
        },
        {
            "op": "create_table",
            "schema": schema_name,
            "name": "items",
            "primary_key": ["item_id"],
            "columns": [
                {"name": "item_id", "type": "bigint", "generator": {"kind": "sequence"}},
                {"name": "cat_id", "type": "bigint", "generator": None},
                {"name": "title", "type": "string", "generator": {"kind": "faker", "provider": "company"}},
                {"name": "price", "type": "decimal(10,2)", "generator": {"kind": "random_decimal", "min": 10, "max": 500}},
            ],
            "foreign_keys": [
                {
                    "columns": ["cat_id"],
                    "ref_schema": schema_name,
                    "ref_table": "categories",
                    "ref_columns": ["cat_id"],
                }
            ],
        },
    ]
    code, res = http("POST", f"{base_url}/sources/{source}/exec", {"operations": init_ops})
    check(f"{source}: exec create schema & tables", code, 200)
    check(f"{source}: executed 3 DDL ops", res.get("executed") if isinstance(res, dict) else None, 3)

    # 4. DML: Наполнение данными (append) с FK связями
    append_ops = [
        {"op": "append", "schema": schema_name, "table": "categories", "rows": 3, "new_key_ratio": 0.0},
        {"op": "append", "schema": schema_name, "table": "items", "rows": 5, "new_key_ratio": 0.0},
    ]
    code, res = http("POST", f"{base_url}/sources/{source}/exec", {"operations": append_ops})
    check(f"{source}: exec append rows", code, 200)

    # 5. DML: Update по предикату
    update_ops = [
        {
            "op": "update",
            "schema": schema_name,
            "table": "categories",
            "predicate": [{"column": "cat_id", "operator": "=", "value": 1}],
            "set": {"cat_name": "PremiumCategory"},
        }
    ]
    code, res = http("POST", f"{base_url}/sources/{source}/exec", {"operations": update_ops})
    check(f"{source}: exec update rows", code, 200)

    # 6. DML: Delete по предикату
    delete_ops = [
        {
            "op": "delete",
            "schema": schema_name,
            "table": "items",
            "predicate": [{"column": "item_id", "operator": "=", "value": 1}],
            "cascade": False,
        }
    ]
    code, res = http("POST", f"{base_url}/sources/{source}/exec", {"operations": delete_ops})
    check(f"{source}: exec delete item", code, 200)

    # 7. Corner-cases:
    # 7a. Блокировка truncate родителя без cascade=true при наличии дочерних строк -> 409
    bad_trunc = [
        {"op": "truncate", "schema": schema_name, "table": "categories", "cascade": False}
    ]
    code, _ = http("POST", f"{base_url}/sources/{source}/exec", {"operations": bad_trunc})
    check(f"{source}: truncate parent without cascade -> 409", code, 409)

    # 7b. Неизвестная колонка в update -> 422
    bad_col_update = [
        {
            "op": "update",
            "schema": schema_name,
            "table": "categories",
            "predicate": [{"column": "unknown_col", "operator": "=", "value": 1}],
            "set": {"cat_name": "test"},
        }
    ]
    code, _ = http("POST", f"{base_url}/sources/{source}/exec", {"operations": bad_col_update})
    check(f"{source}: unknown column update -> 422", code, 422)

    # 7c. Недопустимый оператор в predicate -> 422
    bad_op_update = [
        {
            "op": "update",
            "schema": schema_name,
            "table": "categories",
            "predicate": [{"column": "cat_id", "operator": "ILIKE", "value": "1"}],
            "set": {"cat_name": "test"},
        }
    ]
    code, _ = http("POST", f"{base_url}/sources/{source}/exec", {"operations": bad_op_update})
    check(f"{source}: invalid operator update -> 422", code, 422)

    # 8. Комплексная DDL эволюция: add_column + rename_column
    complex_evolve = [
        {
            "op": "add_column",
            "schema": schema_name,
            "table": "items",
            "column": {"name": "rating", "type": "decimal(3,1)"},
        },
        {
            "op": "rename_column",
            "schema": schema_name,
            "table": "items",
            "from_name": "rating",
            "to_name": "score",
        },
    ]
    code, res = http("POST", f"{base_url}/sources/{source}/exec", {"operations": complex_evolve})
    check(f"{source}: complex evolve (add+rename) -> 200", code, 200)

    # 9. All-or-Nothing Rollback:
    # Батч с корректной операцией и некорректной (пробел в имени колонки)
    failing_batch = [
        {
            "op": "add_column",
            "schema": schema_name,
            "table": "items",
            "column": {"name": "temp_probe", "type": "string"},
        },
        {
            "op": "add_column",
            "schema": schema_name,
            "table": "items",
            "column": {"name": "invalid name with spaces", "type": "string"},
        },
    ]
    code, _ = http("POST", f"{base_url}/sources/{source}/exec", {"operations": failing_batch})
    check(f"{source}: failing batch rejected -> 422", code, 422)

    # Проверка отката: temp_probe отсутствует
    code, schema_meta = http("GET", f"{base_url}/sources/{source}/schema")
    table_obj = schema_meta.get("tables", {}).get(f"{schema_name}.items", {}) if isinstance(schema_meta, dict) else {}
    cols = [c["name"] for c in table_obj.get("columns", [])]
    check(f"{source}: all-or-nothing rollback (temp_probe absent)", "temp_probe" in cols, False)
    check(f"{source}: score column present from step 8", "score" in cols, True)

    # 10. Truncate каскадно (успешная очистка)
    clean_trunc = [
        {"op": "truncate", "schema": schema_name, "table": "categories", "cascade": True}
    ]
    code, res = http("POST", f"{base_url}/sources/{source}/exec", {"operations": clean_trunc})
    check(f"{source}: truncate with cascade -> 200", code, 200)

    print(f"=== Завершено тестирование источника '{source}' ===\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--source", required=True)
    args = parser.parse_args()

    run_source_e2e(args.url, args.source)
    print(f"Итог {args.source}: PASS={PASS} FAIL={FAIL}")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
