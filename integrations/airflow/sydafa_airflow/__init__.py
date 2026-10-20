"""Airflow integration package for sydafa."""

from typing import Any

def get_provider_info() -> dict[str, Any]:
    """Return provider metadata for Apache Airflow."""
    return {
        "package-name": "sydafa-airflow",
        "name": "Sydafa Airflow Integration",
        "description": "Apache Airflow operators and hook for sydafa DBMS emulation service.",
        "versions": ["0.1.0"],
    }


def __getattr__(name: str) -> Any:
    if name == "SydafaHook":
        from sydafa_airflow.hooks.sydafa import SydafaHook
        return SydafaHook
    if name == "SydafaActivateOperator":
        from sydafa_airflow.operators.sydafa import SydafaActivateOperator
        return SydafaActivateOperator
    if name == "SydafaMigrateOperator":
        from sydafa_airflow.operators.sydafa import SydafaMigrateOperator
        return SydafaMigrateOperator
    if name == "SydafaDeactivateOperator":
        from sydafa_airflow.operators.sydafa import SydafaDeactivateOperator
        return SydafaDeactivateOperator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

