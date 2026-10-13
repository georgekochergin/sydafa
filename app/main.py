"""sydafa — фабрика синтетических данных и оркестратор источников данных КХД."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, Response, status

from . import container_manager, exec_engine, generators, schema_engine, schema_state, sources
from .constants import DEFAULT_TTL_MINUTES, IDLE_WATCH_SECONDS, MAX_TTL_MINUTES, SYDAFA_DB
from .exec_engine import ExecError
from .generators import GeneratorError, PredicateError
from .models import (
    AppendRequest,
    DeleteRequest,
    EvolveRequest,
    EvolveResult,
    ExecRequest,
    ExecResult,
    SourceInfo,
    SourceLifecycleStatus,
    TableRef,
    UpdateRequest,
)
from .schema_engine import SchemaEvolutionError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sydafa.main")

_is_ready = False


async def _idle_watcher() -> None:
    while True:
        await asyncio.sleep(IDLE_WATCH_SECONDS)
        try:
            await asyncio.to_thread(container_manager.sweep_idle_sources)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка idle-таймера источников")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _is_ready
    logger.info("Инициализация сервиса sydafa...")
    watcher: asyncio.Task | None = None
    try:
        sources.load_sources()
        _is_ready = True
        logger.info("sydafa готова (источники поднимаются лениво): %s", sources.source_names())
        watcher = asyncio.create_task(_idle_watcher())
    except Exception as e:
        logger.critical("Критическая ошибка при инициализации sydafa: %s", e, exc_info=True)
    yield
    if watcher is not None:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    logger.info("Остановка сервиса sydafa.")


app = FastAPI(
    title="sydafa",
    description="Фабрика синтетических данных, оркестратор СУБД-источников и транзакционная эволюция схем",
    lifespan=lifespan,
)


@app.get("/health")
def healthcheck(response: Response):
    if not _is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "initializing"}
    return {"status": "ok", "database": SYDAFA_DB, "sources": sources.source_names()}


def _assert_source(source: str) -> None:
    if source not in sources.source_names():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Неизвестный источник '{source}'")


def _wake(source: str, ttl_minutes: int = DEFAULT_TTL_MINUTES, publish: bool = False) -> dict:
    try:
        return container_manager.wake_up(source, ttl_minutes=ttl_minutes, publish=publish)
    except Exception as exc:
        logger.exception("Не удалось разбудить источник '%s'", source)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Источник '{source}' не удалось запустить: {exc}") from exc


def _resolve_table(source: str, schema: str, table: str):
    _assert_source(source)
    live = schema_state.get_live_schema(source)
    if schema not in live.schemas:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Схема '{schema}' не найдена в источнике '{source}'")
    key = f"{schema}.{table}"
    if key not in live.tables:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Таблица '{key}' не найдена в источнике '{source}'")
    return live, live.tables[key]


@app.get("/sources")
def list_sources():
    return {"sources": sources.source_names()}


@app.get("/sources/{source}", response_model=SourceInfo)
def get_source_info(source: str, request: Request):
    _assert_source(source)
    _wake(source)
    cfg = sources.get_source(source)
    live = schema_state.get_live_schema(source)
    tables_refs = [TableRef(schema=t.schema_, name=t.name) for t in live.tables.values()]
    request_host = request.url.hostname if request else None
    conn = container_manager.build_connection_details(source, cfg, request_host=request_host)
    return SourceInfo(
        source=source,
        type=cfg.type,
        database=SYDAFA_DB,
        host=cfg.host,
        port=cfg.port,
        reader=cfg.reader,
        schemas=live.schemas,
        tables=tables_refs,
        connection=conn,
    )


@app.get("/sources/{source}/schema")
def get_source_schema(source: str):
    _assert_source(source)
    _wake(source)
    return schema_state.get_live_schema(source).model_dump(by_alias=True)


@app.get("/sources/{source}/status", response_model=SourceLifecycleStatus)
def get_source_status(source: str, request: Request):
    _assert_source(source)
    request_host = request.url.hostname if request else None
    return SourceLifecycleStatus(**container_manager.get_runtime_status(source, request_host=request_host))


@app.post("/sources/{source}/activate", response_model=SourceLifecycleStatus)
def activate_source(
    source: str,
    request: Request,
    ttl_minutes: int | None = Query(default=None, description="Время жизни в минутах (макс. SYDAFA_MAX_TTL_MINUTES, по умолчанию 15)"),
):
    _assert_source(source)
    if ttl_minutes is None or ttl_minutes > MAX_TTL_MINUTES or ttl_minutes <= 0:
        effective_ttl = DEFAULT_TTL_MINUTES
    else:
        effective_ttl = ttl_minutes
    request_host = request.url.hostname if request else None
    return SourceLifecycleStatus(
        **container_manager.wake_up(source, ttl_minutes=effective_ttl, publish=True, request_host=request_host)
    )


@app.post("/sources/{source}/deactivate", response_model=SourceLifecycleStatus)
def deactivate_source(source: str):
    _assert_source(source)
    try:
        return SourceLifecycleStatus(**container_manager.deactivate(source))
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Не удалось деактивировать '{source}': {exc}") from exc


@app.get("/tables/{source}/{schema}/{table}")
def get_table_schema(source: str, schema: str, table: str):
    _, tbl = _resolve_table(source, schema, table)
    return tbl.model_dump(by_alias=True)


@app.post("/sources/{source}/exec", response_model=ExecResult)
def exec_source(source: str, req: ExecRequest):
    _assert_source(source)
    _wake(source)
    try:
        return exec_engine.execute_source_operations(source, req.operations)
    except ExecError as exc:
        code = status.HTTP_409_CONFLICT if exc.is_conflict else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(code, str(exc)) from exc
    except Exception as exc:
        logger.exception("Непредвиденная ошибка при /sources/%s/exec: %s", source, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Ошибка выполнения exec: {exc}") from exc


@app.post("/sources/{source}/schema/evolve", response_model=EvolveResult, deprecated=True)
def evolve_source_schema(source: str, req: EvolveRequest):
    _assert_source(source)
    _wake(source)
    try:
        return schema_engine.execute_evolve(source, req)
    except SchemaEvolutionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:
        logger.error("Непредвиденная ошибка при эволюции схемы '%s': %s", source, exc, exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Ошибка выполнения эволюции: {exc}") from exc


@app.post("/tables/{source}/{schema}/{table}/append", deprecated=True)
def append_table_rows(source: str, schema: str, table: str, req: AppendRequest):
    _wake(source)
    live, _ = _resolve_table(source, schema, table)
    try:
        inserted = generators.append_rows(source, live, schema, table, req.rows, req.new_key_ratio)
        return {"inserted": inserted}
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Ошибка добавления строк: {exc}") from exc


@app.post("/tables/{source}/{schema}/{table}/update", deprecated=True)
def update_table_rows(source: str, schema: str, table: str, req: UpdateRequest):
    _wake(source)
    live, _ = _resolve_table(source, schema, table)
    try:
        updated = generators.update_rows(source, live, schema, table, req.predicate, req.set)
        return {"updated": updated}
    except (PredicateError, GeneratorError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Ошибка обновления: {exc}") from exc


@app.post("/tables/{source}/{schema}/{table}/delete", deprecated=True)
def delete_table_rows(source: str, schema: str, table: str, req: DeleteRequest):
    _wake(source)
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
