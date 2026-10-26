"""Тесты классификации переходных ошибок открытия PDB в provisioning Oracle."""
from __future__ import annotations

import unittest

from app.provisioning import _is_pdb_open_in_progress


class TestIsPdbOpenInProgress(unittest.TestCase):
    def test_transient_open_errors_are_tolerated(self):
        for message in (
            "ORA-65054: database or pluggable database is not open",
            "ORA-65024: Pluggable database SYDAFA is not open.",
            "ORA-65086: pluggable database is already open",
            "DPI-1047: ORA-65024 during open",
        ):
            with self.subTest(message=message):
                self.assertTrue(_is_pdb_open_in_progress(Exception(message)))

    def test_fatal_errors_are_raised(self):
        for message in (
            "ORA-00604: recursive SQL level 1",
            "ORA-65016: FILE_NAME_CONVERT must be specified",
            "",
        ):
            with self.subTest(message=message):
                self.assertFalse(_is_pdb_open_in_progress(Exception(message)))


if __name__ == "__main__":
    unittest.main()
