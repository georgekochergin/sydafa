"""Persist-состояние жизненного цикла контейнера источника."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from .constants import DEFAULT_TTL_MINUTES, STATE_DIR

SourceState = Literal["idle", "starting", "running"]


def _path(source: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"source_status.{source}.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def empty_status(source: str) -> dict[str, Any]:
    return {
        "source": source,
        "state": "idle",
        "last_used": None,
        "activated_until": None,
        "ttl_minutes": DEFAULT_TTL_MINUTES,
        "connection": None,
        "host_port": None,
        "published": False,
    }


def load_status(source: str) -> dict[str, Any]:
    path = _path(source)
    if not path.exists():
        return empty_status(source)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty_status(source)
    base = empty_status(source)
    base.update(raw)
    base["source"] = source
    return base


def save_status(source: str, status: dict[str, Any]) -> dict[str, Any]:
    status = {**status, "source": source}
    _path(source).write_text(json.dumps(status, indent=2, default=str) + "\n", encoding="utf-8")
    return status


def mark_running(
    source: str,
    ttl_minutes: int,
    host_port: int | None,
    published: bool,
    connection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts = now_utc().isoformat()
    until = datetime.fromtimestamp(now_utc().timestamp() + ttl_minutes * 60, tz=timezone.utc).isoformat()
    status = load_status(source)
    status.update(
        {
            "state": "running",
            "last_used": ts,
            "activated_until": until,
            "ttl_minutes": ttl_minutes,
            "connection": connection,
            "host_port": host_port,
            "published": published,
        }
    )
    return save_status(source, status)


def mark_idle(source: str) -> dict[str, Any]:
    status = load_status(source)
    status.update({
        "state": "idle",
        "activated_until": None,
        "connection": None,
        "host_port": None,
        "published": False,
    })
    return save_status(source, status)


def mark_starting(source: str) -> dict[str, Any]:
    status = load_status(source)
    status["state"] = "starting"
    return save_status(source, status)


def is_expired(status: dict[str, Any]) -> bool:
    until = status.get("activated_until")
    if not until or status.get("state") != "running":
        return False
    try:
        deadline = datetime.fromisoformat(until)
    except ValueError:
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return now_utc() >= deadline
