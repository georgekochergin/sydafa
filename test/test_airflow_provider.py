"""Тестирование Airflow провайдера sydafa_airflow (ленивые импорты и метаданные)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Ищем путь к integrations/airflow относительно корня репозитория или в образе
candidates = [
    Path(__file__).resolve().parent.parent / "integrations" / "airflow",
    Path("/app/integrations/airflow"),
]
has_airflow_pkg = False
for c in candidates:
    if c.is_dir():
        if str(c) not in sys.path:
            sys.path.insert(0, str(c))
        has_airflow_pkg = True
        break


@unittest.skipUnless(has_airflow_pkg, "Пакет integrations/airflow не найден в окружении")
class TestAirflowProvider(unittest.TestCase):
    def test_get_provider_info_structure(self):
        import sydafa_airflow

        info = sydafa_airflow.get_provider_info()
        self.assertIsInstance(info, dict)
        self.assertEqual(info.get("package-name"), "sydafa-airflow")
        self.assertEqual(info.get("name"), "Sydafa Airflow Integration")
        self.assertIn("0.1.0", info.get("versions", []))
        self.assertIn("description", info)

    def test_lazy_imports_via_getattr(self):
        from unittest.mock import MagicMock, patch

        mock_airflow = MagicMock()
        mock_hooks_module = MagicMock()
        mock_models_module = MagicMock()
        mock_exceptions_module = MagicMock()

        mock_hooks_module.BaseHook = type("BaseHook", (), {})
        mock_models_module.BaseOperator = type("BaseOperator", (), {})
        mock_exceptions_module.AirflowException = type("AirflowException", (Exception,), {})

        modules_patch = {
            "airflow": mock_airflow,
            "airflow.models": mock_models_module,
            "airflow.exceptions": mock_exceptions_module,
            "airflow.hooks": mock_hooks_module,
            "airflow.hooks.base": mock_hooks_module,
        }

        with patch.dict("sys.modules", modules_patch):
            import sydafa_airflow

            # Проверяем, что операторы и хук доступны через getattr
            hook_cls = getattr(sydafa_airflow, "SydafaHook")
            self.assertEqual(hook_cls.__name__, "SydafaHook")

            act_cls = getattr(sydafa_airflow, "SydafaActivateOperator")
            self.assertEqual(act_cls.__name__, "SydafaActivateOperator")

            mig_cls = getattr(sydafa_airflow, "SydafaMigrateOperator")
            self.assertEqual(mig_cls.__name__, "SydafaMigrateOperator")

            deact_cls = getattr(sydafa_airflow, "SydafaDeactivateOperator")
            self.assertEqual(deact_cls.__name__, "SydafaDeactivateOperator")

    def test_getattr_unknown_attribute_raises(self):
        import sydafa_airflow

        with self.assertRaises(AttributeError) as ctx:
            _ = getattr(sydafa_airflow, "NonExistentAttribute")
        self.assertIn("has no attribute 'NonExistentAttribute'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
