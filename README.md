# sydafa

**sydafa** (Synthetic Data Factory & DB Orchestrator) — автономный сервис для эмуляции СУБД-источников, управления схемами данных и генерации синтетических данных.

## Возможности

- **Автономная оркестрация контейнеров СУБД**: автоматический запуск контейнеров MSSQL и Oracle через Docker Python SDK с инъекцией начальных пользователей и прав без необходимости монтирования внешних хостовых путей.
- **Чистый старт (Clean State)**: сервис стартует с абсолютно чистой базой данных `sydafa`, создаёт технического суперпользователя (`sydafa`/`sydafa`) и пользователя-инспектора только для чтения (`reader`/`reader`).
- **Транзакционная эволюция схем (API-first)**: отсутствие статических файлов схем на хосте. Все DDL-изменения передаются через транзакционный эндпоинт `POST /sources/{source}/schema/evolve` (All-or-Nothing rollback).
- **Поддержка 10 операций миграции**:
  - Схемы: `create_schema`, `drop_schema`, `rename_schema`.
  - Таблицы: `create_table`, `drop_table`, `rename_table`, `transfer_table`.
  - Колонки: `add_column`, `drop_column`, `rename_column`.
- **Генерация данных (DML)**:
  - `POST /tables/{source}/{schema}/{table}/append` — генерация и вставка строк с соблюдением внешних ключей (FK) и типизированных генераторов (`sequence`, `faker`, `choice`, `random_decimal`, `random_date`).
  - `POST /tables/{source}/{schema}/{table}/update` — параметризованное точечное обновление данных с фильтрацией по условиям.
  - `POST /tables/{source}/{schema}/{table}/delete` — удаление строк с условиями или каскадным удалением зависимостей.

## Структура репозитория

```
sydafa/
├── app/                      # Исходный код приложения FastAPI
│   ├── constants.py          # Базовые параметры, реестр образов СУБД
│   ├── container_manager.py  # Оркестрация СУБД контейнеров через Docker SDK
│   ├── db_connectors.py      # Подключения к MSSQL и Oracle
│   ├── generators.py         # DML операции и генераторы синтетики
│   ├── main.py               # REST API эндпоинты
│   ├── models.py             # Pydantic v2 модели операций и параметров
│   ├── provisioning.py       # Создание рабочей базы sydafa и пользователя reader
│   ├── render_ddl.py         # Диалектные трансляторы DDL (MSSQL, Oracle)
│   ├── schema_engine.py      # Движок транзакционных миграций
│   ├── schema_state.py       # Сохранение состояния схемы
│   └── sources.py            # Загрузка источников из YAML
├── templates/init/           # Встроенные bootstrap-скрипты СУБД
│   ├── mssql/entrypoint.sh   # Инициализация логина sydafa/sydafa
│   └── oracle/01-init-sydafa.sql # Создание пользователя sydafa с DBA, SYSDBA
├── tests/                    # Набор unit-тестов
├── examples/sources/         # Примеры конфигурации источников
├── Dockerfile                # Сборка контейнера с запуском тестов
└── requirements.txt          # Python зависимости
```

## Конфигурация источников

Источники описываются в YAML-файлах в директории конфигурации (по умолчанию `/app/config/sources`):

```yaml
type: mssql # или oracle
host: mssql-source
port: 1433
reader: reader
```

## Сборка и запуск

### Сборка Docker-образа

```bash
docker build -t sydafa:latest .
```

При сборке Dockerfile автоматически прогоняет unit-тесты (`python -m unittest discover -s tests`).

### Запуск контейнера

```bash
docker run -d \
  --name sydafa \
  -p 8090:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/config/sources:/app/config/sources:ro \
  -v sydafa_state:/state \
  sydafa:latest
```

## REST API

- `GET /health` — проверка работоспособности сервиса.
- `GET /sources` — список зарегистрированных источников.
- `GET /sources/{source}` — детальные метаданные источника (база, хост, порт, reader).
- `POST /sources/{source}/schema/evolve` — транзакционное применение списка операций эволюции схемы.
- `GET /tables/{source}/{schema}/{table}` — текущая схема таблицы и список колонок.
- `POST /tables/{source}/{schema}/{table}/append` — генерация данных.
- `POST /tables/{source}/{schema}/{table}/update` — обновление данных.
- `POST /tables/{source}/{schema}/{table}/delete` — удаление данных.

Swagger UI доступен по адресу `http://localhost:8090/docs`.
