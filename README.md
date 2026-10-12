# sydafa

**sydafa** (Synthetic Data Factory & DB Orchestrator) — автономный сервис для оркестрации СУБД-источников, декларативного управления схемами данных и генерации синтетических данных.

Проект разработан для изолированного тестирования ETL/ELT-пайплайнов (Spark, Airflow, dbt), озер данных (Iceberg, Delta Lake) и витрин данных без необходимости ручного развертывания и заполнения тестовых реляционных СУБД.

---

## Возможности

- **Автономная оркестрация контейнеров СУБД**: автоматический запуск контейнеров MSSQL и Oracle через Docker Python SDK. Все bootstrap-скрипты инкапсулированы внутри сервиса и не требуют монтирования внешних путей с хоста.
- **Чистый старт (Clean State)**: сервис стартует с чистой рабочей базой `sydafa`, создаёт технического суперпользователя (`sydafa`/`sydafa`) и пользователя-инспектора только для чтения (`reader`/`reader`).
- **Транзакционная эволюция схем (API-first)**: схемы и таблицы не хранятся на хосте в виде статических файлов. Любые DDL-изменения передаются через транзакционный эндпоинт `POST /sources/{source}/schema/evolve` (All-or-Nothing rollback).
- **Поддержка 10 операций миграций**:
  - Схемы: `create_schema`, `drop_schema`, `rename_schema`.
  - Таблицы: `create_table`, `drop_table`, `rename_table`, `transfer_table`.
  - Колонки: `add_column`, `drop_column`, `rename_column`.
- **Генерация данных (DML) с соблюдением целостности связей (FK)**:
  - `POST /tables/{source}/{schema}/{table}/append` — генерация N строк с учетом внешних ключей и типизированных генераторов (`sequence`, `faker`, `choice`, `random_decimal`, `random_date`).
  - `POST /tables/{source}/{schema}/{table}/update` — параметризованное точечное обновление данных с предикатами.
  - `POST /tables/{source}/{schema}/{table}/delete` — удаление строк с проверкой ссылочной целостности или каскадным удалением зависимостей.

---

## Интеграция в сторонние проекты

`sydafa` поставляется в виде независимого Docker-образа и легко встраивается в любой проект, где требуется эмуляция реляционных СУБД (например, хранилища данных, стенды разработки, CI/CD пайплайны).

### 1. Подключение в `docker-compose.yaml`

Добавьте сервис `sydafa` в `docker-compose.yaml` вашего проекта:

```yaml
services:
  sydafa:
    image: ${SYDAFA_IMAGE:-ghcr.io/georgekochergin/sydafa:latest}
    environment:
      MSSQL_SA_PASSWORD: ${MSSQL_SA_PASSWORD:-AdminPassword1!}
    volumes:
      # 1. Доступ к Docker daemon хоста для создания и управления контейнерами СУБД
      - /var/run/docker.sock:/var/run/docker.sock
      # 2. Локальная директория с конфигурацией источников вашего проекта
      - ./config/sydafa/sources:/app/config/sources:ro
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
      - my-network

volumes:
  sydafa_state:

networks:
  my-network:
```

> **Важно по конфигурации**:
> 1. **Монтирование `/var/run/docker.sock`**: сервису необходим доступ к Docker Socket, так как он запускает контейнеры СУБД в той же Docker-сети, к которой подключён сам.
> 2. **Параметр `start_period: 90s` в healthcheck**: создание Pluggable Database (PDB) в Oracle Free и инициализация MSSQL требуют от 20 до 45 секунд. Сервис возвращает `503 Service Unavailable` до тех пор, пока все источники не будут запущены и полностью готовы к приёму соединений, после чего `/health` начинает отдавать `200 OK`.

---

### 2. Конфигурация источников данных

В смонтированной директории (например, `./config/sydafa/sources/`) создаются YAML-файлы для каждого источника.

#### Пример: `mssql.yaml`
```yaml
type: mssql
host: mssql-source
port: 1433
reader: reader
```

#### Пример: `oracle.yaml`
```yaml
type: oracle
host: oracle-source
port: 1521
reader: reader
```

**Пояснение параметров:**
- `type`: диалект СУБД (`mssql` или `oracle`). Образы СУБД зафиксированы внутри сервиса (`mcr.microsoft.com/mssql/server:2022-CU27-ubuntu-22.04` и `gvenzl/oracle-free:23-slim-faststart`).
- `host`: сетевое имя контейнера СУБД в Docker-сети проекта.
- `port`: TCP-порт СУБД (1433 для MSSQL, 1521 для Oracle).
- `reader`: имя пользователя-инспектора только для чтения (пароль совпадает с логином: `reader`/`reader`).

---

### 3. Подключение внешних инструментов и ETL к источникам

При старте сервиса в каждом источнике автоматически создаются:
1. **Рабочая база/PDB**: всегда с именем `sydafa`.
2. **Технический суперпользователь**: `sydafa` / `sydafa` (роль `sysadmin` в MSSQL, `DBA, SYSDBA` в Oracle). Управляет DDL и схемами через API сервиса `sydafa`.
3. **Пользователь-инспектор**: `reader` / `reader`. Обладает правами строго на чтение (`db_datareader` в MSSQL, `SELECT ANY TABLE` в Oracle).

**Подключение из ETL-пайплайнов и DBeaver:**
- **MSSQL**:
  - Host: `localhost` (или `mssql-source` внутри Docker)
  - Port: `1433`
  - Database: `sydafa`
  - User: `reader`, Password: `reader`
  - Параметры драйвера: `encrypt=false;trustServerCertificate=true`
- **Oracle**:
  - Host: `localhost` (или `oracle-source` внутри Docker)
  - Port: `1521`
  - Connection type: Service Name, значение: `sydafa`
  - User: `reader`, Password: `reader`

---

### 4. Очистка контейнеров в тестовых сценариях (CI/CD)

Поскольку `sydafa` запускает контейнеры СУБД динамически через Docker API, команда `docker compose down -v` штатно останавливает только сам сервис `sydafa`. Чтобы гарантированно очистить динамические контейнеры источников, добавьте в ваш скрипт тестирования или CI команду:

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
| GET | `/tables/{source}/{schema}/{table}` | Текущая схема таблицы и список колонок | — |
| POST | `/sources/{source}/schema/evolve` | Транзакционное применение списка DDL-миграций | `{"operations": [...]}` |
| POST | `/tables/{source}/{schema}/{table}/append` | Генерация и вставка N строк с поддержкой связей FK | `{"rows": N, "new_key_ratio": r}` |
| POST | `/tables/{source}/{schema}/{table}/update` | Параметризованное обновление строк по предикату | `{"predicate": [...], "set": {...}}` |
| POST | `/tables/{source}/{schema}/{table}/delete` | Удаление строк по предикату с опциональным каскадом | `{"predicate": [...], "cascade": bool}` |

---

## Транзакционная эволюция схем (POST /sources/{source}/schema/evolve)

Эндпоинт принимает JSON-документ со списком упорядоченных операций. Все операции выполняются строго по очереди в рамках **одной транзакции СУБД**. При ошибке на любом шаге выполняется полный `ROLLBACK` (All-or-Nothing) и возвращается HTTP 422.

### Доступные операции (`op`):

1. **`create_schema`**: создание схемы (MSSQL schema / Oracle user).
   ```json
   {"op": "create_schema", "name": "demo"}
   ```

2. **`create_table`**: создание таблицы со спецификацией колонок, PK и FK.
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

3. **`rename_table`**: переименование таблицы.
   ```json
   {"op": "rename_table", "schema": "demo", "from_name": "customers", "to_name": "clients"}
   ```

4. **`drop_table`**: удаление таблицы.
   ```json
   {"op": "drop_table", "schema": "demo", "name": "customers"}
   ```

5. **`add_column`**: добавление колонки в таблицу.
   ```json
   {"op": "add_column", "schema": "demo", "table": "customers", "column": {"name": "discount", "type": "decimal(5,2)"}}
   ```

6. **`rename_column`**: переименование колонки.
   ```json
   {"op": "rename_column", "schema": "demo", "table": "customers", "from_name": "discount", "to_name": "special_discount"}
   ```

7. **`drop_column`**: удаление колонки.
   ```json
   {"op": "drop_column", "schema": "demo", "table": "customers", "name": "discount"}
   ```

8. **`transfer_table`**: перенос таблицы между схемами.
   ```json
   {"op": "transfer_table", "table": "clients", "from_schema": "demo", "to_schema": "core"}
   ```

9. **`rename_schema`**: переименование схемы с переносом её таблиц.
   ```json
   {"op": "rename_schema", "from_name": "demo", "to_name": "core"}
   ```

10. **`drop_schema`**: удаление пустой схемы.
    ```json
    {"op": "drop_schema", "name": "core"}
    ```

---

## Семантика колонок и генераторов данных (`generator`)

Каждая колонка таблицы описывается объектом со свойствами:
- `name`: имя колонки (латиница, цифры, подчёркивание).
- `type`: логический тип данных:
  - `bigint`: целочисленный 64-битный тип (`BIGINT` в MSSQL, `NUMBER(19)` в Oracle).
  - `string`: строковый тип (`NVARCHAR(255)` в MSSQL, `VARCHAR2(255)` в Oracle).
  - `date`: календарная дата (`DATE`).
  - `timestamp`: дата и время (`DATETIME2` в MSSQL, `TIMESTAMP` в Oracle).
  - `decimal(precision, scale)`: число с фиксированной точностью (`DECIMAL(p,s)` в MSSQL, `NUMBER(p,s)` в Oracle).
- `generator`: конфигурация генератора синтетических данных:
  - **`sequence`** (`{"kind": "sequence"}`): автоинкрементный числовой генератор для первичных ключей (`MAX(pk) + 1`).
  - **`faker`** (`{"kind": "faker", "provider": "company"}`): вызов генераторов библиотеки Faker. Поддерживаемые провайдеры: `company`, `name`, `word`, `address`, `email`.
  - **`choice`** (`{"kind": "choice", "values": ["Retail", "Corporate", "VIP"]}`): выбор случайного значения из списка `values`.
  - **`random_decimal`** (`{"kind": "random_decimal", "min": 1, "max": 1000}`): случайное вещественное число в диапазоне от `min` до `max` с округлением до 2 знаков после запятой.
  - **`random_date`** (`{"kind": "random_date"}`): случайная дата за последние 365 дней.
  - **`null`** (`null`): для колонок внешних ключей (FK), значения которых автоматически подтягиваются из существующей родительской таблицы при генерации.

---

## Манипуляция данными (DML API)

### 1. Добавление данных (POST /tables/{source}/{schema}/{table}/append)

Генерирует и вставляет указанное количество строк с автоматическим соблюдением целостности связей по внешним ключам (FK).

**Тело запроса:**
```json
{
  "rows": 100,
  "new_key_ratio": 0.2
}
```
- `rows`: количество строк для генерации (от 1 до 100 000).
- `new_key_ratio`: вероятность (от 0.0 до 1.0) генерации новой родительской записи по внешнему ключу. Если значение больше 0 и в родительской таблице недостаточно записей, сервис сначала сгенерирует недостающие родительские строки в связанных таблицах.

**Ответ:**
```json
{"status": "ok", "inserted": 100}
```

### 2. Обновление строк по предикату (POST /tables/{source}/{schema}/{table}/update)

Выполняет точечное обновление данных с валидацией колонок и безопасной bind-параметризацией (защита от SQL-инъекций).

**Тело запроса:**
```json
{
  "predicate": [
    {"column": "customer_id", "operator": "=", "value": 10}
  ],
  "set": {
    "segment": "VIP"
  }
}
```
- `predicate`: список условий фильтрации (объединяются через `AND`).
  - `column`: имя существующей колонки таблицы.
  - `operator`: поддерживаемые операторы: `=`, `!=`, `<`, `<=`, `>`, `>=`.
  - `value`: значение фильтра.
- `set`: словарь обновляемых полей `{"имя_колонки": "новое_значение"}`.

**Ответ:**
```json
{"status": "ok", "updated": 1}
```

### 3. Удаление строк по предикату (POST /tables/{source}/{schema}/{table}/delete)

Выполняет удаление строк с защитой внешних ключей или возможностью каскадного удаления зависимостей.

**Тело запроса:**
```json
{
  "predicate": [
    {"column": "customer_id", "operator": "=", "value": 10}
  ],
  "cascade": true
}
```
- `predicate`: список условий фильтрации (объединяются через `AND`).
- `cascade`:
  - `false`: при наличии ссылающихся строк в дочерних таблицах операция будет отклонена с кодом ошибки `409 Conflict`.
  - `true`: каскадно удаляет связанные записи во всех дочерних таблицах перед удалением родительских строк.

**Ответ:**
```json
{"status": "ok", "deleted": 1}
```

---

## Локальная сборка и тестирование

### Сборка образа
```bash
docker build -t sydafa:latest .
```
При сборке Dockerfile автоматически выполняет unit-тесты:
```bash
python -m unittest discover -s tests
```
