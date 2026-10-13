# sydafa

**sydafa** (Synthetic Data Factory & DB Orchestrator) — автономный сервис для оркестрации СУБД-источников, декларативного управления схемами данных и генерации синтетических данных.

Проект разработан для изолированного тестирования ETL/ELT-пайплайнов (Spark, Airflow, dbt), озер данных (Iceberg, Delta Lake) и витрин данных без необходимости ручного развертывания и заполнения тестовых реляционных СУБД.

---

## Возможности

- **Автономная оркестрация контейнеров СУБД**: автоматический запуск контейнеров MSSQL и Oracle через Docker Python SDK. Контейнеры запускаются со штатными образами вендоров без внешних скриптов или модифицированных entrypoint.
- **Чистый старт и авто-инициализация по `init.json`**: сервис создаёт рабочую базу `sydafa`, технического суперпользователя (`sydafa`/`sydafa`) и пользователя-инспектора только для чтения (`reader`/`reader`). При наличии файла `init.json` сервис автоматически создаёт схемы и пустые таблицы при старте.
- **Единый транзакционный метод исполнения `POST /sources/{source}/exec`**: атомарное исполнение батча операций (DDL + DML) в единой транзакции СУБД с гарантией All-or-Nothing.
- **Поддержка DDL-миграций**:
  - Схемы: `create_schema`, `drop_schema`, `rename_schema`.
  - Таблицы: `create_table`, `drop_table`, `rename_table`, `transfer_table`.
  - Колонки: `add_column`, `drop_column`, `rename_column`.
- **Поддержка DML-операций в общем батче**:
  - `truncate`: быстрая и безопасная очистка таблицы с проверкой ссылочной целостности или каскадным разрешением внешних ключей.
  - `append`: генерация N строк с учетом внешних ключей и типизированных генераторов (`sequence`, `faker`, `choice`, `random_decimal`, `random_date`).
  - `update`: параметризованное точечное обновление данных с предикатами.
  - `delete`: удаление строк с проверкой ссылочной целостности или каскадным удалением зависимостей.

---

## Интеграция в сторонние проекты

`sydafa` поставляется в виде независимого Docker-образа и легко встраивается в любой проект, где требуется эмуляция реляционных СУБД.

### 1. Подключение в `docker-compose.yaml`

```yaml
services:
  sydafa:
    image: ${SYDAFA_IMAGE:-ghcr.io/georgekochergin/sydafa:latest}
    environment:
      MSSQL_SA_PASSWORD: ${MSSQL_SA_PASSWORD:-AdminPassword1!}
      ORACLE_PASSWORD: ${ORACLE_PASSWORD:-admin}
    volumes:
      # 1. Доступ к Docker daemon хоста для создания и управления контейнерами СУБД
      - /var/run/docker.sock:/var/run/docker.sock
      # 2. Локальная директория с конфигурацией источников и init.json
      - ./sydafa:/app/config:ro
      # 3. Том для сохранения состояния схемы
      - sydafa_state:/state
    ports:
      - "8090:8000"
    healthcheck:
      test: ["CMD", "python3", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').getcode() == 200 else 1)"]
      interval: 10s
      timeout: 5s
      retries: 30
      start_period: 90s
    networks:
      - dlh-network

volumes:
  sydafa_state:

networks:
  dlh-network:
```

> **Важно по конфигурации**:
> 1. **Монтирование `/var/run/docker.sock`**: сервису необходим доступ к Docker Socket, так как он запускает контейнеры СУБД в той же Docker-сети, к которой подключён сам.
> 2. **Параметр `start_period: 90s` в healthcheck**: инициализация СУБД требует времени. Сервис возвращает `503 Service Unavailable` до тех пор, пока все источники не будут запущены и полностью настроены, после чего `/health` начинает отдавать `200 OK`.

---

### 2. Конфигурация источников данных и `init.json`

В смонтированной директории (например, `./sydafa/`):
- `sources/` — YAML-файлы для каждого источника.
- `init.json` (опционально) — структура исходных схем и таблиц для автоматического создания при старте.

#### Пример: `sydafa/sources/mssql.yaml`
```yaml
type: mssql
host: mssql-source
port: 1433
reader: reader
```

#### Пример: `sydafa/sources/oracle.yaml`
```yaml
type: oracle
host: oracle-source
port: 1521
reader: reader
```

#### Пример: `sydafa/init.json`
```json
{
  "sources": {
    "mssql": [
      {"op": "create_schema", "name": "demo"},
      {
        "op": "create_table",
        "schema": "demo",
        "name": "customers",
        "primary_key": ["customer_id"],
        "columns": [
          {"name": "customer_id", "type": "bigint", "generator": {"kind": "sequence"}},
          {"name": "name", "type": "string", "generator": {"kind": "faker", "provider": "company"}}
        ]
      }
    ]
  }
}
```
Если файл `init.json` отсутствует — сервисы СУБД создаются с чистыми рабочими базами `sydafa`.

---

### 3. Подключение внешних инструментов и ETL к источникам

При старте сервиса в каждом источнике автоматически настраиваются:
1. **Рабочая база/PDB**: всегда с именем `sydafa`.
2. **Технический суперпользователь**: `sydafa` / `sydafa` (роль `sysadmin` в MSSQL, `DBA, SYSDBA` в Oracle).
3. **Пользователь-инспектор**: `reader` / `reader` (только чтение: `db_datareader` в MSSQL, `SELECT ANY TABLE` в Oracle).

**Подключение из ETL-пайплайнов и DBeaver:**
- **MSSQL**: Host: `mssql-source` (или `localhost`), Port: `1433`, Database: `sydafa`, User/Password: `reader`/`reader`.
- **Oracle**: Host: `oracle-source` (или `localhost`), Port: `1521`, Service Name: `sydafa`, User/Password: `reader`/`reader`.

---

### 4. Очистка контейнеров в тестовых сценариях (CI/CD)

Так как `sydafa` запускает контейнеры СУБД динамически через Docker API, для гарантированной очистки динамических контейнеров добавьте в скрипт очистки:

```bash
docker rm -f mssql-source oracle-source >/dev/null 2>&1 || true
```

---

## REST API сервиса

Интерактивная документация Swagger UI доступна по адресу: `http://localhost:8090/docs`.

| Метод | Путь | Назначение | Тело запроса |
| --- | --- | --- | --- |
| GET | `/health` | Проверка готовности сервиса (200 OK при готовности всех источников) | — |
| GET | `/sources` | Список зарегистрированных источников | — |
| GET | `/sources/{source}` | Реквизиты источника, статус reader и список схем/таблиц | — |
| GET | `/sources/{source}/schema` | Текущая live-схема источника (JSON) | — |
| GET | `/tables/{source}/{schema}/{table}` | Текущая спецификация таблицы и список колонок | — |
| POST | `/sources/{source}/exec` | Единый метод транзакционного выполнения DDL + DML операций | `{"operations": [...]}` |

---

## Единый метод исполнения операций (POST /sources/{source}/exec)

Эндпоинт принимает JSON-документ со списком упорядоченных операций. Все операции (DDL и DML) выполняются строго по очереди в рамках **одной транзакции СУБД**. При ошибке на любом шаге выполняется полный `ROLLBACK` (All-or-Nothing) и возвращается HTTP 422 (или 409 при конфликте связей FK).

### 1. DDL операции (`op`):
- `create_schema`: `{"op": "create_schema", "name": "demo"}`
- `create_table`:
  ```json
  {
    "op": "create_table",
    "schema": "demo",
    "name": "customers",
    "primary_key": ["customer_id"],
    "columns": [
      {"name": "customer_id", "type": "bigint", "generator": {"kind": "sequence"}},
      {"name": "name", "type": "string", "generator": {"kind": "faker", "provider": "company"}},
      {"name": "segment", "type": "string", "generator": {"kind": "choice", "values": ["Retail", "Corporate", "VIP"]}}
    ],
    "foreign_keys": []
  }
  ```
- `add_column`: `{"op": "add_column", "schema": "demo", "table": "customers", "column": {"name": "discount", "type": "decimal(5,2)"}}`
- `rename_column`: `{"op": "rename_column", "schema": "demo", "table": "customers", "from_name": "discount", "to_name": "special_discount"}`
- `drop_column`: `{"op": "drop_column", "schema": "demo", "table": "customers", "name": "special_discount"}`
- `rename_table`: `{"op": "rename_table", "schema": "demo", "from_name": "customers", "to_name": "clients"}`
- `drop_table`: `{"op": "drop_table", "schema": "demo", "name": "customers"}`
- `transfer_table`: `{"op": "transfer_table", "table": "clients", "from_schema": "demo", "to_schema": "core"}`
- `rename_schema`: `{"op": "rename_schema", "from_name": "demo", "to_name": "core"}`
- `drop_schema`: `{"op": "drop_schema", "name": "core"}`

### 2. DML операции (`op`):
- `truncate`:
  ```json
  {"op": "truncate", "schema": "demo", "table": "orders", "cascade": true}
  ```
  *(Если `cascade=false`, а на таблицу ссылаются дочерние записи — операция отклоняется с HTTP 409 Conflict)*.
- `append`:
  ```json
  {"op": "append", "schema": "demo", "table": "customers", "rows": 10, "new_key_ratio": 0.0}
  ```
- `update`:
  ```json
  {
    "op": "update",
    "schema": "demo",
    "table": "customers",
    "predicate": [{"column": "customer_id", "operator": "=", "value": 1}],
    "set": {"segment": "VIP"}
  }
  ```
- `delete`:
  ```json
  {
    "op": "delete",
    "schema": "demo",
    "table": "customers",
    "predicate": [{"column": "customer_id", "operator": "=", "value": 1}],
    "cascade": false
  }
  ```

---

## Поддерживаемые типы данных (маппинг в СУБД)

При объявлении колонок в таблицах используются логические типы, транслируемые в диалекты СУБД:

| Логический тип | MSSQL DDL | Oracle DDL | Spark/Iceberg тип |
|---|---|---|---|
| `bigint` | `BIGINT` | `NUMBER(19)` | `long` |
| `string` | `NVARCHAR(400)` | `VARCHAR2(400)` | `string` |
| `decimal(p,s)` | `DECIMAL(p,s)` | `NUMBER(p,s)` | `decimal(p,s)` |
| `date` | `DATE` | `DATE` | `date` |
| `timestamp` | `DATETIME2` | `TIMESTAMP` | `timestamp` |

---

## Тестирование сервиса

Репозиторий `sydafa` поддерживает два уровня тестирования:

1. **Быстрые unit-тесты (внутри Dockerfile при сборке)**:
   ```bash
   docker build -t sydafa:latest .
   ```
   Прогоняет 17 unit-тестов API, Pydantic-моделей, DDL-транслятора и генератора DAG-ов без поднятия тяжелых СУБД.

2. **Сквозное последовательное e2e-тестирование СУБД в Docker**:
   ```bash
   ./test/test.sh --e2e
   # Или полный прогон (unit + e2e):
   ./test/test.sh
   ```
   Последовательно поднимает реальный MSSQL, прогоняет полный цикл API (DDL, DML append, update, delete, truncate, 409 FK cascade, 422 валидации, All-or-Nothing откат), гасит MSSQL; затем поднимает реальный Oracle, прогоняет те же проверки и очищает окружение.
