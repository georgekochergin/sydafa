"""Генератор демонстрационных Airflow DAG-ов загрузки ODS на основе схемы init.json."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from app import schema_state, sources

logger = logging.getLogger("sydafa.dag_generator")


def _render_ods_dag(source: str, schema: str) -> str:
    """Рендерит шаблон Airflow DAG для загрузки ODS конкретной схемы источника."""
    dag_id = f"ods_load_{source}_{schema}"
    job_script = f"/opt/spark-jobs/load_ods_{source}.py"

    return f'''"""Автоматически сгенерированный демо-DAG {dag_id}.
Сгенерирован сервисом sydafa на основе метаданных схемы источника '{source}', схема '{schema}'.
По умолчанию DAG выключен (is_paused_upon_creation=True).
"""
from airflow import DAG
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
from airflow.providers.standard.operators.python import PythonOperator


def _validate_load_placeholder(**_) -> None:
    print("[validate_load] placeholder: метод валидации (см. docs/README.md)")


with DAG(
    dag_id="{dag_id}",
    schedule=None,
    catchup=False,
    is_paused_upon_creation=True,
    tags=["ods", "{source}", "{schema}", "demo"],
) as dag:
    extract_load = SparkSubmitOperator(
        task_id="extract_load",
        application="{job_script}",
        conn_id="spark_default",
        deploy_mode="client",
    )

    validate_load = PythonOperator(
        task_id="validate_load",
        python_callable=_validate_load_placeholder,
    )

    extract_load >> validate_load
'''


def generate_ods_dags(init_content: dict[str, Any] | None = None) -> dict[str, str]:
    """Генерирует словарь { 'filename': 'содержимое_файла' } для ODS DAG-ов.

    Если init_content передан, парсит схемы и таблицы из него.
    Иначе обращается к live_schema зарегистрированных источников.
    Если таблиц нет, возвращает пустой словарь.
    """
    dags: dict[str, str] = {}
    source_schemas: dict[str, set[str]] = {}

    if init_content:
        sources_dict = init_content.get("sources", init_content)
        if isinstance(sources_dict, dict):
            for src, ops in sources_dict.items():
                if not isinstance(ops, list):
                    continue
                for op in ops:
                    if isinstance(op, dict):
                        op_type = op.get("op")
                        if op_type == "create_table":
                            s = op.get("schema", "demo")
                            source_schemas.setdefault(src, set()).add(s)
    else:
        # Из LiveSchema
        for src in sources.source_names():
            live = schema_state.get_live_schema(src)
            for tbl in live.tables.values():
                source_schemas.setdefault(src, set()).add(tbl.schema_)

    for src, schemas in source_schemas.items():
        for schema in sorted(schemas):
            filename = f"ods_load_{src}_{schema}.py"
            dags[filename] = _render_ods_dag(src, schema)

    return dags


def export_dags_to_directory(target_dir: Path, init_path: Path | None = None) -> list[str]:
    """Генерирует и записывает файлы DAG-ов в указанную директорию."""
    init_content = None
    if init_path and init_path.is_file():
        try:
            init_content = json.loads(init_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("Не удалось прочитать init_path %s: %s", init_path, e)

    dags = generate_ods_dags(init_content)
    target_dir.mkdir(parents=True, exist_ok=True)
    created_files: list[str] = []

    for filename, code in dags.items():
        file_path = target_dir / filename
        file_path.write_text(code, encoding="utf-8")
        created_files.append(str(file_path))
        logger.info("Сгенерирован DAG файл: %s", file_path)

    return created_files


def main():
    parser = argparse.ArgumentParser(description="Генератор Airflow DAG-ов на основе схемы sydafa")
    parser.add_argument("--output-dir", required=True, help="Директория для сохранения DAG-ов (например, airflow/dags/demo)")
    parser.add_argument("--init", default=None, help="Путь к init.json файлу (опционально)")
    args = parser.parse_args()

    init_file = Path(args.init) if args.init else None
    out_dir = Path(args.output_dir)
    created = export_dags_to_directory(out_dir, init_file)
    print(f"Успешно создано {len(created)} DAG файлов в {out_dir}")


if __name__ == "__main__":
    main()
