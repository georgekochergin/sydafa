"""Архитектурные guard-ы: границы слоёв после рефакторинга source-адаптеров.

Проверяются импорты и ветвления в исходниках конкретных модулей (не глобальный
запрет строк «mssql»/«oracle»: они правомерны в реестре, enum, реализациях,
тестах и документации).
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Общие модули: вендорная диспетчеризация запрещена
COMMON_MODULES = [
    "app/main.py",
    "app/container_manager.py",
    "app/sources.py",
    "app/provisioning.py",
    "app/init_apply.py",
    "app/constants.py",
    "app/naming.py",
    "app/secrets.py",
    "app/source_status.py",
    "app/schema_state.py",
    "app/rules_state.py",
    "app/backends/registry.py",
    "app/backends/contracts.py",
    "app/backends/rdbms/contracts.py",
    "app/backends/rdbms/ddl.py",
    "app/backends/rdbms/schema.py",
    "app/backends/rdbms/execution.py",
    "app/backends/rdbms/generation.py",
    "app/backends/rdbms/mutations.py",
    "app/backends/rdbms/data.py",
    "app/backends/rdbms/initialization.py",
]

VENDOR_FILES = list((REPO / "app/backends/rdbms/mssql").glob("*.py")) + \
    list((REPO / "app/backends/rdbms/oracle").glob("*.py"))


def _src(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _imported_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class TestVendorDispatchBoundaries(unittest.TestCase):
    def test_common_modules_have_no_vendor_branching(self):
        """В общих модулях нет ветвлений/строк конкретных движков."""
        for rel in COMMON_MODULES:
            with self.subTest(module=rel):
                src = _src(rel)
                self.assertNotIn('"mssql"', src)
                self.assertNotIn("'mssql'", src)
                self.assertNotIn('"oracle"', src)
                self.assertNotIn("'oracle'", src)

    def test_container_manager_has_no_db_drivers(self):
        """Docker-менеджер не импортирует драйверы СУБД."""
        src = _src("app/container_manager.py")
        self.assertNotIn("pymssql", src)
        self.assertNotIn("oracledb", src)

    def test_pure_generation_has_no_io_layer_imports(self):
        """Чистая генерация значений без FastAPI/Docker/драйверов/backends/state."""
        src = _src("app/generation/values.py")
        names = _imported_names(src)
        forbidden = {
            "fastapi", "uvicorn", "docker", "pymssql", "oracledb",
            "app",  # любой внутренний слой, кроме models, недопустим
        }
        self.assertFalse(names & forbidden, f"Запрещённые импорты: {sorted(names & forbidden)}")
        self.assertNotIn("backends", src)
        self.assertNotIn("schema_state", src)

    def test_vendor_modules_do_not_import_common_infrastructure(self):
        """Реализации движков не зависят от container_manager/main (направление зависимостей)."""
        for path in VENDOR_FILES:
            with self.subTest(module=path.name):
                src = path.read_text(encoding="utf-8")
                names = _imported_names(src)
                self.assertNotIn("docker", names)
                # относительные импорты вверх к общим инфраструктурным модулям запрещены
                self.assertNotIn("from ....container_manager", src)
                self.assertNotIn("from .....container_manager", src)
                self.assertNotIn("from ....main", src)
                self.assertNotIn("import ....container_manager", src)

    def test_airflow_package_has_no_service_internals(self):
        """Интеграционный пакет — только HTTP-клиент, без внутренних app.* импортов."""
        airflow = REPO / "integrations/airflow/sydafa_airflow"
        for path in airflow.rglob("*.py"):
            with self.subTest(module=str(path.relative_to(REPO))):
                src = path.read_text(encoding="utf-8")
                self.assertNotIn("from app.", src)
                self.assertNotIn("import app\n", src)
                self.assertNotIn("backends", src)


if __name__ == "__main__":
    unittest.main()
