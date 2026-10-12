"""Unit-тесты генератора Airflow DAG-ов sydafa."""
import unittest
from app.dag_generator import generate_ods_dags


class TestDagGenerator(unittest.TestCase):
    def test_empty_init_generates_no_dags(self):
        dags = generate_ods_dags({})
        self.assertEqual(dags, {})

    def test_generate_ods_dags_from_init(self):
        init_data = {
            "sources": {
                "mssql": [
                    {"op": "create_schema", "name": "demo"},
                    {
                        "op": "create_table",
                        "schema": "demo",
                        "name": "customers",
                        "primary_key": ["id"],
                        "columns": [{"name": "id", "type": "bigint"}],
                    },
                ],
                "oracle": [
                    {"op": "create_schema", "name": "crm"},
                    {
                        "op": "create_table",
                        "schema": "crm",
                        "name": "leads",
                        "primary_key": ["id"],
                        "columns": [{"name": "id", "type": "bigint"}],
                    },
                ],
            }
        }
        dags = generate_ods_dags(init_data)
        self.assertIn("ods_load_mssql_demo.py", dags)
        self.assertIn("ods_load_oracle_crm.py", dags)

        mssql_code = dags["ods_load_mssql_demo.py"]
        self.assertIn('dag_id="ods_load_mssql_demo"', mssql_code)
        self.assertIn("is_paused_upon_creation=True", mssql_code)
        self.assertIn("load_ods_mssql.py", mssql_code)

        oracle_code = dags["ods_load_oracle_crm.py"]
        self.assertIn('dag_id="ods_load_oracle_crm"', oracle_code)
        self.assertIn("is_paused_upon_creation=True", oracle_code)
        self.assertIn("load_ods_oracle.py", oracle_code)


if __name__ == "__main__":
    unittest.main()
