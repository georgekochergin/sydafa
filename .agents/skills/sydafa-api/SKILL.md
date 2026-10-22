---
name: sydafa-api
description: Use when the user asks to generate JSON request bodies for the sydafa API (create/drop/rename schemas and tables, add/drop/rename columns, transfer tables, append/update/delete/truncate rows, configure generation rules) from natural-language requirements. Produces the Swagger UI link, the target endpoint, and a ready JSON body for POST /sources/{source}/migrate or /sources/{source}/rules/{entity_fqn}.
---

# sydafa API: генерация JSON-тел запросов

Этот скилл переводит требования пользователя на естественном языке
(например, «создай таблицу с двумя колонками и 10 записями», «удали таблицу», «переименуй колонку», «обнови 5 случайных строк»)
в готовый валидный JSON для sydafa API.

> **Единая точка правды (Single Source of Truth):**
> Каноническая проектная документация, полное описание всех 14 операций, поддерживаемых типов (`bigint`, `string`, `decimal(p,s)`, `date`, `timestamp`),
> параметров генераторов (`sequence`, `uuid`, `faker`, `choice`, `random_decimal`, `random_date`, `date_offset`, а также `null_ratio`, `weights`, `locale`, `dim`),
> поведения таблиц аудита (`_upd_`, `_del_`) и правил валидации зафиксированы в основном файле проекта: [README.md](../../../README.md) (Раздел 7 «Транзакционная эволюция схем и управление данными (`POST /sources/{source}/migrate`)»).
> Настоящий скилл ссылается на `README.md` и служит исключительно для быстрой сборки JSON-спецификаций из текста.

## Формат ответа (обязательный)

На каждую задачу выводи ровно в таком порядке:

1. **Ссылка на Swagger UI** — корень документации sydafa. Порт берётся из
   `docker-compose.yaml` (сервис `sydafa`, поле `ports` → host-порт, например `8090`).
   Не хардкодь порт: найди его в актуальном `docker-compose.yaml` проекта.
   Формат: `http://localhost:<host-порт>/docs`.

2. **Целевая ручка API** согласно актуальному коду:
   - оркестрация инстансов: `POST /sources` (создание `{"name": "...", "type": "..."}`);
   - данные и эволюция схемы: `POST /sources/{source}/migrate` (единый транзакционный DDL + DML метод);
   - профили правил и инвариантов генерации: `PUT /sources/{source}/rules/{entity_fqn}` (установка правил для сущности `schema.table`);
   - инспекция из DBeaver: `POST /sources/{source}/activate?ttl_minutes=15`
     (в ответе объект `connection`); статус — `GET /sources/{source}/status`;
     остановка — `POST /sources/{source}/deactivate`.

3. **JSON-тело для вставки** в Swagger UI.

## Целевой эндпоинт миграций: `POST /sources/{source}/migrate`

Принимает `{"operations": [...]}` — упорядоченный батч DDL и DML, который
выполняется в единой транзакции (All-or-Nothing). Операции выполняются строго последовательно в порядке списка:
поддерживается любое логичное чередование DDL и DML (например: `create_table` → первичное наполнение `append` → эволюция `add_column` → `update`).
Важно лишь соблюдать естественную причинную зависимость: схема создается до таблицы, таблица — до строк в ней, колонка — до операций с ней.

`{source}` — имя источника (`mssql`, `oracle` или имя инстанса, например `billing`, `crm`).

## Эндпоинт правил связности данных: `PUT /sources/{source}/rules/{entity_fqn}`

Принимает профиль правил генерации `TableRuleProfile` (управляет как генераторами колонок `columns`, так и инвариантами таблицы `invariants`).
Опциональный параметр: `?validate_existing=false` (или поле в инварианте).
- `false` (по умолчанию, мягкий режим): мутации `update`/`delete` свободны (позволяют вносить тестовые аномалии), а последующий `append` автоматически балансирует счет пополнениями.
- `true` (строгий режим): исторические данные проверяются с ошибкой 422, а мутации `update`/`delete` блокируются Invariant Guard при нарушении баланса.

```json
{
  "columns": {
    "account_id": {"kind": "choice", "values": [101, 102, 103]},
    "client_id": {"kind": "choice", "values": [1, 2, 3]}
  },
  "invariants": [
    {
      "kind": "cumulative_balance",
      "partition_by": ["account_id"],
      "amount_column": "amount",
      "min_balance": 0.0,
      "deposit_prob": 0.5,
      "deposit_range": [100.0, 5000.0],
      "withdrawal_range": [50.0, 2000.0],
      "validate_existing": false
    }
  ]
}
```

## Сводная матрица поддерживаемых операций (`operations[].op`)

Детальные примеры и схемы каждого объекта описаны в `README.md` (раздел 9).

### 1. DDL — Эволюция схем и таблиц (10 операций)

| Операция (`op`) | Обязательные поля | Назначение / Ограничение |
|---|---|---|
| `create_schema` | `name` | Создание новой схемы в базе |
| `drop_schema` | `name` | Удаление пустой схемы (ошибка, если в ней есть таблицы) |
| `rename_schema` | `from_name`, `to_name` | Переименование схемы с переносом таблиц |
| `create_table` | `entity_fqn`, `primary_key`, `columns`, `foreign_keys` (с `ref_entity_fqn`) | Создание таблицы с типами данных |
| `drop_table` | `entity_fqn` | Удаление таблицы (каскадный drop constraints) |
| `rename_table` | `entity_fqn`, `to_name` | Переименование таблицы внутри схемы |
| `transfer_table` | `entity_fqn`, `to_schema` | Перенос таблицы в другую схему |
| `add_column` | `entity_fqn`, `column` (объект `ColumnSpec`) | Добавление новой колонки |
| `drop_column` | `entity_fqn`, `name` | Удаление колонки (запрещено удалять колонки из PK) |
| `rename_column` | `entity_fqn`, `from_name`, `to_name` | Переименование колонки (PK обновляется автоматически) |

### 2. DML — Данные и генерация (4 операции)

| Операция (`op`) | Обязательные поля | Опциональные поля | Назначение |
|---|---|---|---|
| `truncate` | `entity_fqn` | `cascade` (bool) | Очистка таблицы |
| `append` | `entity_fqn`, `rows` | — | Синтетическое наполнение по генераторам колонок (значения FK берутся исключительно из существующих строк родителя) |
| `update` | `entity_fqn`, `set` | `predicate` (list), `limit` (int) | Обновление строк по предикату и/или не более N случайных строк |
| `delete` | `entity_fqn` | `predicate` (list), `cascade` (bool), `limit` (int) | Удаление строк по предикату и/или не более N случайных строк |

## Системные колонки и таблицы аудита (_batch_id, _exec_batch_id, _upd_, _del_)

Каждая создаваемая таблица автоматически получает служебные колонки и пустые таблицы аудита.

### Системные колонки

| Колонка | Где присутствует | Назначение |
|---|---|---|
| `_batch_id` | В каждой основной таблице (внедряется автоматически) | Идентификатор батча/инкремента, которым была создана строка. Выставляется при `append` и перезаписывается актуальным батчем при `update`. |
| `_exec_batch_id` | В таблицах аудита `_upd_<table>` и `_del_<table>` | Идентификатор батча, выполнившего операцию `update`/`delete`/`truncate`. |

Правила работы с системными колонками:
- **Нельзя** указывать `_batch_id` или `_exec_batch_id` в `columns`/`primary_key` операции `create_table` и в `column` операции `add_column` — они игнорируются или отклоняются как системные.
- **Запрещено** удалять (`drop_column`) или переименовывать (`rename_column`) `_batch_id` и `_exec_batch_id` (ошибка).
- В `set` операции `update` системные колонки игнорируются: `_batch_id` обновляется движком автоматически.

### Таблицы аудита

| Таблица | Назначение |
|---|---|
| `_upd_<table>` | Снимки строк ДО `update`: копируются изменяемые строки с `_exec_batch_id = <текущий батч>` и сохранённым прежним `_batch_id`. Равенство: `main._batch_id == _upd_<table>._exec_batch_id`. |
| `_del_<table>` | Снимки удалённых/очищенных строк: перед `delete` и `truncate` строки копируются с `_exec_batch_id = <текущий батч>` (при `cascade=true` — также дочерние таблицы). |

Обе таблицы аудита создаются пустыми вместе с основной таблицей и **автоматически пересоздаются пустыми** при эволюции схемы (`add_column`, `drop_column`, `rename_column`), а также синхронизируются при `drop_table`, `rename_table`, `transfer_table`.

## Правила генерации JSON-батча

1. **Идентификаторы**: имена схем, таблиц и колонок должны удовлетворять регулярному выражению `^[A-Za-z_][A-Za-z0-9_]*$`.
2. **Иерархия создания**:
   - `create_schema` обязательно должно предшествовать `create_table` в этой схеме в рамках одного батча.
   - При `transfer_table` целевая схема `to_schema` должна быть создана ранее.
3. **Объект колонки в `add_column`**: поле `column` всегда передается как единичный объект `{"name": "...", "type": "..."}`, а не массив.
4. **Наполнение данными**: требование «добавь N записей» реализуется строго через операцию `append` с указанием `rows: N`.
5. **Ограничения целостности**:
   - Не удалять колонки, входящие в `primary_key`.
   - Не удалять схему через `drop_schema`, если перед этим не были удалены (`drop_table`) или перенесены (`transfer_table`) все таблицы из нее.
   - Предикаты в `update` и `delete` принимают список условий: `{"column": "...", "operator": "=", "value": ...}`. Допустимые операторы: `=`, `!=`, `>`, `<`, `>=`, `<=`. Если предикат опущен или пуст, операция применяется ко всем строкам таблицы (или ко всей таблице с учетом `limit`).
   - Опция `limit: N` в `update` и `delete` задаёт ограничение «не более N случайных строк» для мутации. Если подходящих строк меньше N, обрабатываются все найденные строки без падений.

## Пример полного сценария

Требование: «в источнике billing создай схему core, таблицу accounts с id и balance, добавь 50 записей, а затем переименуй колонку balance в amount».

```json
{
  "operations": [
    {"op": "create_schema", "name": "core"},
    {
      "op": "create_table",
      "entity_fqn": "core.accounts",
      "primary_key": ["id"],
      "columns": [
        {"name": "id", "type": "bigint"},
        {"name": "balance", "type": "decimal(18,2)"}
      ],
      "foreign_keys": []
    },
    {"op": "append", "entity_fqn": "core.accounts", "rows": 50},
    {"op": "rename_column", "entity_fqn": "core.accounts", "from_name": "balance", "to_name": "amount"}
  ]
}
```