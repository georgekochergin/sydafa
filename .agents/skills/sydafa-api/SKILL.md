---
name: sydafa-api
description: Use when the user asks to generate JSON request bodies for the sydafa API (create/drop/rename schemas and tables, add/drop/rename columns, transfer tables, append/update/delete/truncate rows) from natural-language requirements. Produces the Swagger UI link, the target endpoint, and a ready JSON body for POST /sources/{source}/exec.
---

# sydafa API: генерация JSON-тел запросов

Этот скилл переводит требования пользователя на естественном языке
(например, «создай таблицу с двумя колонками и 10 записями», «удали таблицу», «переименуй колонку»)
в готовый JSON для sydafa API.

> **Каноническая спецификация операций и моделей данных:**
> Полное описание всех 14 операций, поддерживаемых типов (`bigint`, `string`, `decimal(p,s)`, `date`, `timestamp`),
> параметров генераторов (`sequence`, `faker`, `choice`, `random_decimal`, `random_date`) и правил валидации
> находится в основном файле документации проекта: [README.md](../../../README.md) (Раздел 9 «Единый метод исполнения: `POST /sources/{source}/exec`»).

## Формат ответа (обязательный)

На каждую задачу выводи ровно в таком порядке:

1. **Ссылка на Swagger UI** — корень документации sydafa. Порт берётся из
   `docker-compose.yaml` (сервис `sydafa`, поле `ports` → host-порт, например `8090`).
   Не хардкодь порт: найди его в актуальном `docker-compose.yaml` проекта.
   Формат: `http://localhost:<host-порт>/docs`.

2. **Целевая ручка API** согласно актуальному коду:
   - оркестрация инстансов: `POST /sources` (создание `{"name": "...", "type": "..."}`);
   - данные и эволюция схемы: `POST /sources/{source}/exec` (единый транзакционный DDL + DML метод);
   - инспекция из DBeaver: `POST /sources/{source}/activate?ttl_minutes=15`
     (в ответе объект `connection`); статус — `GET /sources/{source}/status`;
     остановка — `POST /sources/{source}/deactivate`.

3. **JSON-тело для вставки** в Swagger UI.

## Целевой эндпоинт: `POST /sources/{source}/exec`

Принимает `{"operations": [...]}` — упорядоченный батч DDL и DML, который
выполняется в единой транзакции (All-or-Nothing). Порядок операций имеет
значение: сначала DDL (создание схем, таблиц, колонок, переименования), затем DML (`append`, `update`, `delete`, `truncate`).

`{source}` — имя источника (`mssql`, `oracle` или имя инстанса, например `billing`, `crm`).

## Сводная матрица поддерживаемых операций (`operations[].op`)

Детальные примеры и схемы каждого объекта описаны в `README.md` (раздел 9).

### 1. DDL — Эволюция схем и таблиц (10 операций)

| Операция (`op`) | Обязательные поля | Назначение / Ограничение |
|---|---|---|
| `create_schema` | `name` | Создание новой схемы в базе |
| `drop_schema` | `name` | Удаление пустой схемы (ошибка, если в ней есть таблицы) |
| `rename_schema` | `from_name`, `to_name` | Переименование схемы с переносом таблиц |
| `create_table` | `schema`, `name`, `primary_key`, `columns`, `foreign_keys` | Создание таблицы с типами и генераторами |
| `drop_table` | `schema`, `name` | Удаление таблицы (каскадный drop constraints) |
| `rename_table` | `schema`, `from_name`, `to_name` | Переименование таблицы в схеме |
| `transfer_table` | `table`, `from_schema`, `to_schema` | Перенос таблицы в другую схему |
| `add_column` | `schema`, `table`, `column` (объект `ColumnSpec`) | Добавление новой колонки |
| `drop_column` | `schema`, `table`, `name` | Удаление колонки (запрещено удалять колонки из PK) |
| `rename_column` | `schema`, `table`, `from_name`, `to_name` | Переименование колонки (PK обновляется автоматически) |

### 2. DML — Данные и генерация (4 операции)

| Операция (`op`) | Обязательные поля | Опциональные поля | Назначение |
|---|---|---|---|
| `truncate` | `schema`, `table` | `cascade` (bool) | Очистка таблицы |
| `append` | `schema`, `table`, `rows` | `new_key_ratio` (float) | Синтетическое наполнение по генераторам колонок |
| `update` | `schema`, `table`, `predicate`, `set` | — | Обновление строк по предикату |
| `delete` | `schema`, `table`, `predicate` | `cascade` (bool) | Удаление строк по предикату |

## Правила генерации JSON-батча

1. **Идентификаторы**: имена схем, таблиц и колонок должны удовлетворять регулярному выражению `^[A-Za-z_][A-Za-z0-9_]*$`.
2. **Иерархия создания**:
   - `create_schema` обязательно должно предшествовать `create_table` в этой схеме в рамках одного батча.
   - При `transfer_table` целевая схема `to_schema` должна быть создана ранее.
3. **Объект колонки в `add_column`**: поле `column` всегда передается как единичный объект `{"name": "...", "type": "...", "generator": {...}}`, а не массив.
4. **Наполнение данными**: требование «добавь N записей» реализуется строго через операцию `append` с указанием `rows: N`.
5. **Ограничения целостности**:
   - Не удалять колонки, входящие в `primary_key`.
   - Не удалять схему через `drop_schema`, если перед этим не были удалены (`drop_table`) или перенесены (`transfer_table`) все таблицы из нее.
   - Предикаты в `update` и `delete` принимают список условий: `{"column": "...", "operator": "=", "value": ...}`. Допустимые операторы: `=`, `!=`, `>`, `<`, `>=`, `<=`.

## Пример полного сценария

Требование: «в источнике billing создай схему core, таблицу accounts с id и balance, добавь 50 записей, а затем переименуй колонку balance в amount».

```json
{
  "operations": [
    {"op": "create_schema", "name": "core"},
    {
      "op": "create_table",
      "schema": "core",
      "name": "accounts",
      "primary_key": ["id"],
      "columns": [
        {"name": "id", "type": "bigint", "generator": {"kind": "sequence"}},
        {"name": "balance", "type": "decimal(18,2)", "generator": {"kind": "random_decimal", "min": 100, "max": 10000}}
      ],
      "foreign_keys": []
    },
    {"op": "append", "schema": "core", "table": "accounts", "rows": 50, "new_key_ratio": 0.0},
    {"op": "rename_column", "schema": "core", "table": "accounts", "from_name": "balance", "to_name": "amount"}
  ]
}
```