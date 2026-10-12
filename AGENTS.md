# sydafa — Инструкции проекта

## Контекст и архитектура
- Автономный сервис эмуляции СУБД, оркестрации контейнеров через Docker SDK, транзакционной эволюции схем данных (API-first) и синтетической генерации данных (FK-aware).
- Стек: Python 3.12, FastAPI, Uvicorn, Pydantic v2, pymssql, oracledb, Faker, Docker Python SDK.

## Git
- Все новые изменения оформляются в отдельных ветках с префиксом `kodik/` (например, `kodik/feature-name`).
- Запрещено коммитить напрямую в `main`.

## Testing
- Запуск unit-тестов внутри Dockerfile при сборке:
  `docker build -t sydafa:latest .`
- Локальный запуск unit-тестов (при наличии зависимостей):
  `python -m unittest discover -s tests`
- Критерий успешности: 100% прохождение тестов (Ran N tests ... OK) и успешный билд образа.
