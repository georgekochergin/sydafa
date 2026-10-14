---
name: sydafa-api
description: Use when the user asks to generate JSON request bodies for the sydafa API (create schemas/tables, add columns, append/update/delete rows) from natural-language requirements. Produces the Swagger UI link, the target endpoint, and a ready JSON body for POST /sources/{source}/exec.
---

# sydafa API: генерация JSON-тел запросов

Этот скилл переводит требования пользователя на естественном языке
(например, «создай таблицу с двумя колонками и 10 записями») в готовый
JSON для sydafa API.

## Формат ответа (обязательный)

На каждую задачу выводи ровно в таком порядке:

1. **Ссылка на Swagger UI** — корень документации sydafa. Порт берётся из
   `docker-compose.yaml` sidafa (сервис `sydafa`, поле `ports` → host-порт).
   Не хардкодь порт: найди его в актуальном `docker-compose.yaml` проекта.
   Формат: `http://localhost:<host-порт>/docs`.

2. **Целевая ручка API** согласно актуальному коду:
   - оркестрация инстансов: `POST /sources` (создание `{"name": "...", "type": "..."}`);
   - данные/схема: `POST /sources/{source}/exec` (основной транзакционный DDL+DML);
   - инспекция из DBeaver: `POST /sources/{source}/activate?ttl_minutes=15`
     (в ответе объект `connection`); статус — `GET /sources/{source}/status`;
     остановка — `POST /sources/{source}/deactivate`.

3. **JSON-тело для вставки** в Swagger UI.

## Целевой эндпоинт: `POST /sources/{source}/exec`

Принимает `{"operations": [...]}` — упорядоченный батч DDL и DML, который
выполняется в одной транзакции (All-or-Nothing). Порядок операций имеет
значение: сначала DDL, затем DML.

`{source}` — имя источника (`mssql`, `oracle`).

## Lifecycle (инспекция)

Для «подключись из DBeaver» сначала `POST /sources/{source}/activate?ttl_minutes=15`,
затем в ответе взять объект `connection` (host, port, database, user, password, jdbc_url).
Публичные реквизиты инспектора/читателя: пользователь `sydafa`, пароль `sydafa`. Админ СУБД снаружи недоступен.

## Логические типы колонок (`columns[].type`)

| Тип | Назначение |
|---|---|
| `bigint` | целочисленный ключ / счётчик |
| `string` | строка (MSSQL `NVARCHAR(400)`, Oracle `VARCHAR2(400)`) |
| `decimal(p,s)` | десятичное с точностью `p` и масштабом `s` |
| `date` | дата |
| `timestamp` | дата+время |

## Генераторы значений (`columns[].generator`)

Поле `generator` содержит объект с обязательным ключом `kind`:

| `kind` | Поля | Что генерирует |
|---|---|---|
| `sequence` | — | последовательный PK (`bigint`) |
| `faker` | `provider`: `company`, `name`, `word`, `address`, `email` | синтетическая строка |
| `choice` | `values`: `["Retail","Corporate","VIP"]` | одно значение из списка |
| `random_decimal` | `min`, `max` (float) | случайное число |
| `random_date` | — | дата за последний год |

Генератор можно опустить (`null`) — тогда колонка останется пустой.

## DDL-операции

### create_schema
```json
{"op": "create_schema", "name": "demo"}
```

### create_table
```json
{
  "op": "create_table",
  "schema": "demo",
  "name": "users",
  "primary_key": ["id"],
  "columns": [
    {"name": "id", "type": "bigint", "generator": {"kind": "sequence"}},
    {"name": "email", "type": "string", "generator": {"kind": "faker", "provider": "email"}}
  ],
  "foreign_keys": []
}
```
Поля: `schema` (уже созданная схема), `name`, `primary_key` (список колонок),
`columns` (список), `foreign_keys` (опционально).
Формат FK: `{"columns": ["user_id"], "ref_table": "users", "ref_columns": ["id"], "ref_schema": "demo"}`.

### Остальные DDL
- `add_column`: `{"op":"add_column","schema":"demo","table":"users","column":{"name":"age","type":"bigint"}}`
- `drop_column`: `{"op":"drop_column","schema":"demo","table":"users","name":"age"}`
- `rename_column`: `{"op":"rename_column","schema":"demo","table":"users","from_name":"age","to_name":"years"}`
- `rename_table`: `{"op":"rename_table","schema":"demo","from_name":"users","to_name":"clients"}`
- `drop_table`: `{"op":"drop_table","schema":"demo","name":"users"}`
- `transfer_table`: `{"op":"transfer_table","table":"users","from_schema":"demo","to_schema":"core"}`
- `rename_schema`: `{"op":"rename_schema","from_name":"demo","to_name":"core"}`
- `drop_schema`: `{"op":"drop_schema","name":"demo"}`

## DML-операции

### truncate
```json
{"op": "truncate", "schema": "demo", "table": "orders", "cascade": true}
```

### append (наполнение «N записями»)
```json
{"op": "append", "schema": "demo", "table": "users", "rows": 10, "new_key_ratio": 0.0}
```
`rows` — сколько строк добавить (`1..100000`), `new_key_ratio` — доля новых FK
(`0.0..1.0`). Требование «N записей» всегда реализуется через `append`, а не
через отдельные insert.

### update
```json
{
  "op": "update",
  "schema": "demo",
  "table": "users",
  "predicate": [{"column": "id", "operator": "=", "value": 1}],
  "set": {"name": "VIP"}
}
```

### delete
```json
{
  "op": "delete",
  "schema": "demo",
  "table": "users",
  "predicate": [{"column": "id", "operator": "=", "value": 1}],
  "cascade": false
}
```

Предикат — список условий `{"column","operator","value"}`; операторы:
`=`, `!=`, `>`, `<`, `>=`, `<=`.

## Правила генерации

- Идентификаторы (схемы, таблицы, колонки) — латиница, цифры, `_`,
  первым символом буква или `_` (`[A-Za-z_][A-Za-z0-9_]*`).
- `create_schema` должно идти ДО `create_table` в том же батче — иначе
  «схема не задекларирована».
- `add_column` принимает поле `column` как **объект** `ColumnSpec`
  (`{"name","type","generator"}`), а не список.
- Наполнение таблицы = операция `append` c `"rows": N`.
- DDL выполняется до DML; все операции — в одном списке `operations`.

## Пример полного сценария

Требование: «в источнике mssql создай схему demo, таблицу users с id и email
и добавь 10 записей».

```json
{
  "operations": [
    {"op": "create_schema", "name": "demo"},
    {
      "op": "create_table",
      "schema": "demo",
      "name": "users",
      "primary_key": ["id"],
      "columns": [
        {"name": "id", "type": "bigint", "generator": {"kind": "sequence"}},
        {"name": "email", "type": "string", "generator": {"kind": "faker", "provider": "email"}}
      ]
    },
    {"op": "append", "schema": "demo", "table": "users", "rows": 10, "new_key_ratio": 0.0}
  ]
}
```