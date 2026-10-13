# sydafa — Инструкции проекта

## Контекст и архитектура
- Автономный сервис эмуляции СУБД, оркестрации контейнеров через Docker SDK, транзакционной эволюции схем данных (API-first) и синтетической генерации данных (FK-aware).
- Стек: Python 3.12, FastAPI, Uvicorn, Pydantic v2, pymssql, oracledb, Faker, Docker Python SDK.

## Git
- Все новые изменения оформляются в отдельных ветках с префиксом `kodik/` (например, `kodik/feature-name`).
- Запрещено коммитить напрямую в `main`.
- Обязательно создавай Pull Request и возвращай ссылки на pull request (-ы) в финальном резюме по доработкам.

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
