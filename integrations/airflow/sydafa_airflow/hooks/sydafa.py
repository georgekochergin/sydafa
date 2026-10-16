"""Sydafa hook for interacting with sydafa REST API."""
from __future__ import annotations

import logging
from typing import Any
import urllib.parse

import requests
from airflow.exceptions import AirflowException
from airflow.hooks.base import BaseHook

logger = logging.getLogger("airflow.providers.sydafa")


class SydafaHook(BaseHook):
    """Хук для взаимодействия с REST API sydafa через Airflow Connection.

    Аргумент conn_id является строго обязательным.
    """

    conn_name_attr = "conn_id"
    conn_type = "http"
    hook_name = "Sydafa"

    def __init__(self, conn_id: str) -> None:
        super().__init__()
        if not conn_id:
            raise ValueError("Параметр 'conn_id' обязателен для инициализации SydafaHook")
        self.conn_id = conn_id

    def get_base_url(self) -> str:
        """Извлекает и нормализует базовый URL из Airflow Connection."""
        conn = self.get_connection(self.conn_id)
        host = (conn.host or "").strip()
        if not host:
            raise AirflowException(f"В подключении '{self.conn_id}' не указан host")

        if host.startswith("http://") or host.startswith("https://"):
            parsed = urllib.parse.urlsplit(host)
            scheme = parsed.scheme
            netloc = parsed.netloc
            path = parsed.path
        else:
            scheme = (conn.schema or "http").strip()
            netloc = host
            path = ""

        if conn.port and ":" not in netloc:
            netloc = f"{netloc}:{conn.port}"

        base_url = f"{scheme}://{netloc}{path}".rstrip("/")
        return base_url

    def activate(
        self,
        source: str,
        ttl_minutes: int = 15,
        timeout_seconds: int = 180,
    ) -> dict[str, Any]:
        """Явная активация СУБД источника и продление TTL (POST /sources/{source}/activate)."""
        base_url = self.get_base_url()
        url = f"{base_url}/sources/{source}/activate"
        params = {"ttl_minutes": ttl_minutes}

        self.log.info(
            "Активация источника '%s' в sydafa (%s, ttl_minutes=%d, conn_id='%s')...",
            source,
            url,
            ttl_minutes,
            self.conn_id,
        )

        try:
            resp = requests.post(url, params=params, timeout=timeout_seconds)
        except requests.RequestException as exc:
            raise AirflowException(
                f"Ошибка сетевого соединения с sydafa по адресу {url}: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise AirflowException(
                f"Ошибка активации источника '{source}' в sydafa (HTTP {resp.status_code}): {resp.text}"
            )

        try:
            payload: dict[str, Any] = resp.json()
        except Exception as exc:
            raise AirflowException(
                f"Некорректный JSON в ответе sydafa при активации '{source}': {resp.text}"
            ) from exc

        self.log.info(
            "Источник '%s' успешно активирован (state=%s, activated_until=%s)",
            source,
            payload.get("state"),
            payload.get("activated_until"),
        )
        return payload

    def migrate(
        self,
        source: str,
        operations: list[dict[str, Any]],
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        """Транзакционное применение DDL/DML операций к источнику (POST /sources/{source}/migrate)."""
        base_url = self.get_base_url()
        url = f"{base_url}/sources/{source}/migrate"
        body = {"operations": operations}

        self.log.info(
            "Выполнение миграции для источника '%s' в sydafa (%s, operations=%d, conn_id='%s')...",
            source,
            url,
            len(operations),
            self.conn_id,
        )

        try:
            resp = requests.post(url, json=body, timeout=timeout_seconds)
        except requests.RequestException as exc:
            raise AirflowException(
                f"Ошибка сетевого соединения с sydafa по адресу {url}: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise AirflowException(
                f"Ошибка выполнения миграции для '{source}' в sydafa (HTTP {resp.status_code}): {resp.text}"
            )

        try:
            payload: dict[str, Any] = resp.json()
        except Exception as exc:
            raise AirflowException(
                f"Некорректный JSON в ответе sydafa при миграции '{source}': {resp.text}"
            ) from exc

        self.log.info(
            "Миграция источника '%s' успешно завершена (executed=%s)",
            source,
            payload.get("executed"),
        )
        return payload

    def deactivate(
        self,
        source: str,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        """Принудительная остановка СУБД источника для освобождения RAM (POST /sources/{source}/deactivate)."""
        base_url = self.get_base_url()
        url = f"{base_url}/sources/{source}/deactivate"

        self.log.info(
            "Деактивация источника '%s' в sydafa (%s, conn_id='%s')...",
            source,
            url,
            self.conn_id,
        )

        try:
            resp = requests.post(url, timeout=timeout_seconds)
        except requests.RequestException as exc:
            raise AirflowException(
                f"Ошибка сетевого соединения с sydafa по адресу {url}: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise AirflowException(
                f"Ошибка деактивации источника '{source}' в sydafa (HTTP {resp.status_code}): {resp.text}"
            )

        try:
            payload: dict[str, Any] = resp.json()
        except Exception as exc:
            raise AirflowException(
                f"Некорректный JSON в ответе sydafa при деактивации '{source}': {resp.text}"
            ) from exc

        self.log.info(
            "Источник '%s' успешно деактивирован (state=%s)",
            source,
            payload.get("state"),
        )
        return payload
