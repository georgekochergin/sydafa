"""Airflow integration package for sydafa."""

from typing import Any

from sydafa_airflow.hooks.sydafa import SydafaHook
from sydafa_airflow.operators.sydafa import (
    SydafaActivateOperator,
    SydafaDeactivateOperator,
    SydafaMigrateOperator,
)

__all__ = [
    "SydafaHook",
    "SydafaActivateOperator",
    "SydafaMigrateOperator",
    "SydafaDeactivateOperator",
    "get_provider_info",
]


def get_provider_info() -> dict[str, Any]:
    """Return provider metadata for Apache Airflow."""
    return {
        "package-name": "sydafa-airflow",
        "name": "Sydafa Airflow Integration",
        "description": "Apache Airflow operators and hook for sydafa DBMS emulation service.",
        "versions": ["0.1.0"],
    }
