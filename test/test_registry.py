"""Тесты статического реестра движков и границы импортов backend-слоя."""
from __future__ import annotations

import subprocess
import sys
import unittest
import typing

from app.backends import registry
from app.backends.contracts import FAMILY_RDBMS
from app.backends.registry import UnknownEngineError
from app.models import SourceConfig


def _public_enum_types() -> tuple[str, ...]:
    hints = typing.get_type_hints(SourceConfig)
    return tuple(sorted(typing.get_args(hints["type"])))


class TestBackendRegistry(unittest.TestCase):
    def test_known_engines_registered(self):
        self.assertEqual(registry.registered_types(), ("mssql", "oracle"))

    def test_enum_matches_registry(self):
        """Значения Literal в публичных моделях и ключи реестра — одно множество."""
        self.assertEqual(set(registry.registered_types()), set(_public_enum_types()))

    def test_require_known_backends(self):
        for engine in ("mssql", "oracle"):
            backend = registry.require(engine)
            self.assertEqual(backend.engine_type, engine)
            self.assertEqual(backend.family, FAMILY_RDBMS)
            self.assertTrue(backend.internal_port > 0)
            self.assertIsNotNone(backend.runtime)

    def test_require_unknown_fails_without_fallback(self):
        """Неизвестный тип — явный отказ; никаких «не MSSQL -> Oracle»."""
        for bad in ("postgres", "oracl", "", "db2"):
            with self.subTest(engine=bad):
                with self.assertRaises(UnknownEngineError):
                    registry.require(bad)

    def test_unknown_engine_error_lists_supported(self):
        with self.assertRaises(UnknownEngineError) as ctx:
            registry.require("db2")
        self.assertIn("mssql", str(ctx.exception))
        self.assertIn("oracle", str(ctx.exception))

    def test_require_normalizes_case_and_spaces(self):
        self.assertEqual(registry.require(" Oracle ").engine_type, "oracle")
        self.assertTrue(registry.has_engine("MSSQL"))

    def test_require_rdbms_returns_family_backends(self):
        for engine in ("mssql", "oracle"):
            backend = registry.require_rdbms(engine)
            self.assertEqual(backend.family, FAMILY_RDBMS)

    def test_duplicate_registration_is_error(self):
        from app.backends.registry import _register

        class DupBackend:
            engine_type = "mssql"
            family = FAMILY_RDBMS
            internal_port = 1433
            runtime = None

        with self.assertRaises(RuntimeError):
            _register(DupBackend())


class TestRegistryImportBoundary(unittest.TestCase):
    def test_import_backends_has_no_side_effect_dependencies(self):
        """Импорт реестра не тянет Docker SDK, драйверы СУБД и FastAPI."""
        code = (
            "import sys; import app.backends;"
            "leaked = {'docker', 'pymssql', 'oracledb', 'fastapi', 'uvicorn'} & set(sys.modules);"
            "assert not leaked, f'Утечка импортов: {sorted(leaked)}'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd="/app",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
