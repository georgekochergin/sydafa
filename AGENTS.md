# sydafa — Инструкции проекта

## Контекст и архитектура
- Автономный сервис эмуляции СУБД, оркестрации контейнеров через Docker SDK, транзакционной эволюции схем данных (API-first) и синтетической генерации данных (FK-aware).
- Стек: Python 3.12, FastAPI, Uvicorn, Pydantic v2, pymssql, oracledb, Faker, Docker Python SDK.
- Источники поднимаются лениво (`wake_up`): не в `lifespan`. Данные — named volume `{prefix}{source}-data`; секреты — `/state/secrets.json`; статус — `/state/source_status.{source}.json`.
- Два рабочих аккаунта: `sydafa` (рандомный пароль, только сервис) и `reader` (пароль = имя). `sa` DISABLE, `pdbadmin` DROP.
- Host-порт публикуется только в `POST /activate` и возвращается в `host_port`.
- `init.json` применяется один раз, если `live_schema` пуста (`app/init_apply.py`).
- `/health` = готовность API, не прогрев СУБД.

## Git
- Все новые изменения оформляются в отдельных ветках с префиксом `kodik/` (например, `kodik/feature-name`).
- Запрещено коммитить напрямую в `main`.
- Обязательно создавай Pull Request и возвращай ссылки на pull request (-ы) в финальном резюме по доработкам.

## Скилл sydafa-api
- При любых изменениях в коде, затрагивающих REST API (ручки, модели запросов, операции `op`, типы колонок, генераторы), проверяй актуальность скилла `.agents/skills/sydafa-api/SKILL.md` и обновляй его при необходимости.

## Testing
- Стандартный сценарий полной проверки сервиса (unit + e2e):
  `./test/test.sh`
- Запуск только сквозного e2e-тестирования СУБД (MSSQL -> затем Oracle) в Docker:
  `./test/test.sh --e2e`
- Запуск тестирования конкретного источника:
  `./test/test.sh --source mssql` или `./test/test.sh --source oracle`
- Запуск быстрых unit-тестов внутри Dockerfile при сборке:
  `./test/test.sh --unit` (или `docker build -t sydafa:latest .`)
- Критерий успешности: 100% прохождение тестов (PASS всех этапов, exit-code 0).
