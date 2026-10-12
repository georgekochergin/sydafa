"""sydafa — фабрика синтетических данных и оркестратор источников данных КХД."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Response, status

from . import container_manager, generators, provisioning, schema_engine, schema_state, sources
from .constants import SYDAFA_DB
from .generators import GeneratorError, PredicateError
from .models import (
    AppendRequest,
    DeleteRequest,
    EvolveRequest,
    EvolveResult,
    SourceInfo,
    TableRef,
    UpdateRequest,
)
from .schema_engine import SchemaEvolutionError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sydafa.main")

_is_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _is_ready
    logger.info("Инициализация сервиса sydafa...")
    try:
        # 1. Загрузка реквизитов источников
        sources.load_sources()
        # 2. Оркестрация контейнеров источников (Docker SDK)
        container_manager.ensure_all_source_containers()
        # 3. Provisioning чистой базы sydafa и пользователя reader
        provisioning.provision_all_sources()
        _is_ready = True
        logger.info("sydafa успешно запущена и готова к работе.")
    except Exception as e:
        logger.critical("Критическая ошибка при инициализации sydafa: %s", e, exc_info=True)
        # Не блокируем старт процесса FastAPI, но healthcheck останется 503
    yield
    logger.info("Остановка сервиса sydafa.")


app = FastAPI(
    title="sydafa",
    description="Фабрика синтетических данных, оркестратор СУБД-источников и транзакционная эволюция схем",
    lifespan=lifespan,
)


@app.get("/health")
def healthcheck(response: Response):
    """Healthcheck эндпоинт для Compose и downstream-сервисов."""
    if not _is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "initializing"}
    return {"status": "ok", "database": SYDAFA_DB, "sources": sources.source_names()}


def _assert_source(source: str) -> None:
    if source not in sources.source_names():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Неизвестный источник '{source}'")


def _resolve_table(source: str, schema: str, table: str):
    _assert_source(source)
    live = schema_state.get_live_schema(source)
    if schema not in live.schemas:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Схема '{schema}' не найдена в источнике '{source}'")
    key = f"{schema}.{table}"
    if key not in live.tables:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Таблица '{key}' не найдена в источнике '{source}'")
    return live, live.tables[key]


# --- Интроспекция источников ---

@app.get("/sources")
def list_sources():
    """Список всех настроенных источников."""
    return {"sources": sources.source_names()}


@app.get("/sources/{source}", response_model=SourceInfo)
def get_source_info(source: str):
    """Метаданные источника: реквизиты, параметры подключения и список схем/таблиц."""
    _assert_source(source)
    cfg = sources.get_source(source)
    live = schema_state.get_live_schema(source)

    tables_refs = [
        TableRef(schema=t.schema_, name=t.name)
        for t in live.tables.values()
    ]

    return SourceInfo(
        source=source,
        type=cfg.type,
        database=SYDAFA_DB,
        host=cfg.host,
        port=cfg.port,
        reader=cfg.reader,
        schemas=live.schemas,
        tables=tables_refs,
    )


@app.get("/sources/{source}/schema")
def get_source_schema(source: str):
    """Полная live-схема данных источника в формате JSON."""
    _assert_source(source)
    return schema_state.get_live_schema(source).model_dump(by_alias=True)


@app.get("/tables/{source}/{schema}/{table}")
def get_table_schema(source: str, schema: str, table: str):
    """Спецификация конкретной таблицы источника."""
    _, tbl = _resolve_table(source, schema, table)
    return tbl.model_dump(by_alias=True)


# --- Эволюция схемы (DDL) ---

@app.post("/sources/{source}/schema/evolve", response_model=EvolveResult)
def evolve_source_schema(source: str, req: EvolveRequest):
    """Единая транзакционная ручка эволюции схемы (All-or-Nothing)."""
    _assert_source(source)
    try:
        result = schema_engine.execute_evolve(source, req)
        return result
    except SchemaEvolutionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:
        logger.error("Непредвиденная ошибка при эволюции схемы '%s': %s", source, exc, exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Ошибка выполнения эволюции: {exc}") from exc


# --- Манипуляция данными (DML) ---

@app.post("/tables/{source}/{schema}/{table}/append")
def append_table_rows(source: str, schema: str, table: str, req: AppendRequest):
    """Генерация и добавление новых строк в таблицу с FK-каскадом."""
    live, _ = _resolve_table(source, schema, table)
    try:
        inserted = generators.append_rows(source, live, schema, table, req.rows, req.new_key_ratio)
        return {"inserted": inserted}
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Ошибка добавления строк: {exc}") from exc


@app.post("/tables/{source}/{schema}/{table}/update")
def update_table_rows(source: str, schema: str, table: str, req: UpdateRequest):
    """Обновление строк таблицы по предикату условий."""
    live, _ = _resolve_table(source, schema, table)
    try:
        updated = generators.update_rows(source, live, schema, table, req.predicate, req.set)
        return {"updated": updated}
    except (PredicateError, GeneratorError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Ошибка обновления: {exc}") from exc


@app.post("/tables/{source}/{schema}/{table}/delete")
def delete_table_rows(source: str, schema: str, table: str, req: DeleteRequest):
    """Удаление строк таблицы по предикату (точечное или каскадное)."""
    live, _ = _resolve_table(source, schema, table)
    try:
        deleted = generators.delete_rows(source, live, schema, table, req.predicate, req.cascade)
        return {"deleted": deleted}
    except PredicateError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except GeneratorError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Ошибка удаления: {exc}") from exc
