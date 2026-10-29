"""Тонкая точка dispatch идемпотентного provisioning: выбор backend по типу источника.

Весь вендорный bootstrap (рабочая БД, DBA, публичный reader) живёт в
`backends/rdbms/<engine>/runtime.py`; здесь только сбор контекста вызова.
"""
from __future__ import annotations

import logging

from .backends import registry
from .backends.contracts import ReadyContext
from .container_manager import container_name_for
from .secrets import get_secrets
from .sources import all_sources, get_source

logger = logging.getLogger("sydafa.provisioning")


def provision_source(source_name: str) -> None:
    config = get_source(source_name)
    backend = registry.require(config.type)
    ctx = ReadyContext(
        config=config,
        secrets=get_secrets(),
        is_initialized=False,
        endpoint_host=container_name_for(config),
    )
    backend.runtime.provision(ctx)


def provision_all_sources() -> None:
    for name in all_sources():
        provision_source(name)
