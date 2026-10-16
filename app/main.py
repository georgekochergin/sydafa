"""sydafa — фабрика синтетических данных и оркестратор источников данных КХД."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, Response, status

from . import container_manager, exec_engine, rules_state, schema_state, sources
from .constants import DEFAULT_TTL_MINUTES, IDLE_WATCH_SECONDS, MAX_TTL_MINUTES, SYDAFA_DB
from .exec_engine import ExecError
from .models import (
    LiveSchema,
    MigrateRequest,
    MigrateResult,
    SourceInfo,
    SourceLifecycleStatus,
    SourceReinitResult,
    TableRef,
    TableRuleProfile,
)

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
        from . import init_apply
        init_apply.check_and_heal_orphaned_state()
        sources.load_sources()
        init_apply.seed_all_sources_on_first_startup()
        _is_ready = True
        logger.info(
            "sydafa готова (первичная инициализация завершена, СУБД остановлены в простое, initialized_at=%s): %s",
            init_apply.get_init_timestamp(),
            sources.source_names(),
        )
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
    from . import init_apply
    if not _is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "initializing", "detail": "Инициализация сервиса и первичное развертывание источников..."}
    return {
        "status": "ok",
        "database": SYDAFA_DB,
        "sources": sources.source_names(),
        "initialized_at": init_apply.get_init_timestamp(),
    }


def _assert_source(source: str) -> None:
    if source not in sources.source_names():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Неизвестный источник '{source}'")


def _wake(source: str, ttl_minutes: int = DEFAULT_TTL_MINUTES, publish: bool = False, request_host: str | None = None) -> dict:
    try:
        return container_manager.wake_up(source, ttl_minutes=ttl_minutes, publish=publish, request_host=request_host)
    except Exception as exc:
        logger.exception("Не удалось разбудить источник '%s'", source)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Источник '{source}' не удалось запустить: {exc}") from exc


# ==================== ORCHESTRATION API (Инфраструктура источников) ====================

@app.get("/sources")
def list_sources():
    """Возвращает список всех источников, зарегистрированных в манифесте sources.json."""
    return {"sources": sources.source_names()}


@app.get("/sources/{source}", response_model=SourceInfo)
def get_source_info(source: str, request: Request):
    _assert_source(source)
    request_host = request.url.hostname if request else None
    cfg = sources.get_source(source)
    live = schema_state.get_live_schema(source)
    tables_refs = [TableRef(schema=t.schema_, name=t.name) for t in live.tables.values()]
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
    return schema_state.get_live_schema(source).model_dump(by_alias=True)


# ==================== LIFECYCLE API (Жизненный цикл контейнеров) ====================

@app.get("/sources/{source}/status", response_model=SourceLifecycleStatus)
def get_source_status(source: str, request: Request):
    _assert_source(source)
    request_host = request.url.hostname if request else None
    return SourceLifecycleStatus(**container_manager.get_runtime_status(source, request_host=request_host))


@app.post("/sources/{source}/activate", response_model=SourceLifecycleStatus)
def activate_source(
    source: str,
    request: Request,
    ttl_minutes: int | None = Query(default=None, description="Время жизни в минутах (макс. SYDAFA_MAX_TTL_MINUTES, дефолт 15)"),
):
    _assert_source(source)
    if ttl_minutes is None or ttl_minutes <= 0:
        effective_ttl = DEFAULT_TTL_MINUTES
    elif ttl_minutes > MAX_TTL_MINUTES:
        effective_ttl = MAX_TTL_MINUTES
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


@app.post("/sources/{source}/reinit", response_model=SourceReinitResult)
def reinit_source_endpoint(source: str):
    """Сбрасывает контейнер, том данных и повторно выполняет начальное развертывание из sources.json."""
    _assert_source(source)
    try:
        from . import init_apply
        res = init_apply.reinit_source(source)
        return SourceReinitResult(**res)
    except Exception as exc:
        logger.exception("Ошибка реинициализации источника '%s'", source)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Не удалось реинициализировать '{source}': {exc}") from exc


# ==================== DATA & SCHEMA MIGRATION API (Транзакционный DDL + DML) ====================

@app.post("/sources/{source}/migrate", response_model=MigrateResult)
@app.post("/sources/{source}/exec", response_model=MigrateResult, include_in_schema=False)
def migrate_source(source: str, req: MigrateRequest, request: Request):
    """Выполняет упорядоченный батч операций DDL и DML в единой All-or-Nothing транзакции."""
    _assert_source(source)
    request_host = request.url.hostname if request else None
    _wake(source, request_host=request_host)
    try:
        return exec_engine.execute_source_operations(source, req.operations)
    except ExecError as exc:
        code = status.HTTP_409_CONFLICT if exc.is_conflict else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(code, str(exc)) from exc
    except Exception as exc:
        logger.exception("Непредвиденная ошибка при /sources/%s/migrate: %s", source, exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Ошибка выполнения migrate: {exc}") from exc


# ==================== DATA GENERATION RULES API (Профили правил данных) ====================

@app.get("/sources/{source}/rules", response_model=dict[str, TableRuleProfile])
def list_source_rules(source: str):
    """Возвращает все зарегистрированные профили правил генерации для источника."""
    _assert_source(source)
    return rules_state.get_all_rules(source)


@app.get("/sources/{source}/rules/{table_fqn:path}", response_model=TableRuleProfile)
def get_table_rule_profile(source: str, table_fqn: str):
    """Возвращает профиль правил генерации конкретной таблицы schema.table."""
    _assert_source(source)
    try:
        profile = rules_state.get_table_rules(source, table_fqn)
    except ValueError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Для таблицы '{table_fqn}' правила генерации не найдены")
    return profile


@app.put("/sources/{source}/rules/{table_fqn:path}", response_model=TableRuleProfile)
def set_table_rule_profile(
    source: str,
    table_fqn: str,
    profile: TableRuleProfile,
    validate_existing: bool = Query(
        default=False,
        description="Если True, проверяет соответствие существующих данных в СУБД правилам и падает с 422 при нарушениях. По умолчанию False (мягкий режим).",
    ),
    request: Request = None,
):
    """Устанавливает или перезаписывает профиль правил генерации для таблицы schema.table."""
    _assert_source(source)
    try:
        if validate_existing:
            for inv in profile.invariants:
                if hasattr(inv, "validate_existing"):
                    inv.validate_existing = True
            request_host = request.url.hostname if request else None
            _wake(source, request_host=request_host)
            rules_state.validate_table_invariants_on_data(source, table_fqn, profile)
        elif any(getattr(inv, "validate_existing", False) for inv in profile.invariants):
            request_host = request.url.hostname if request else None
            _wake(source, request_host=request_host)
            rules_state.validate_table_invariants_on_data(source, table_fqn, profile)
        rules_state.set_table_rules(source, table_fqn, profile)
    except ValueError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err
    return profile


@app.delete("/sources/{source}/rules/{table_fqn:path}")
def delete_table_rule_profile(source: str, table_fqn: str):
    """Сбрасывает профиль правил генерации таблицы schema.table."""
    _assert_source(source)
    try:
        deleted = rules_state.delete_table_rules(source, table_fqn)
    except ValueError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Для таблицы '{table_fqn}' правила генерации не найдены")
    return {"status": "ok", "deleted": table_fqn}
