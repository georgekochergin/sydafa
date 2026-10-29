"""Семейство RDBMS: общие алгоритмы миграции/генерации и реализации движков.

Общий слой (ddl, execution, generation, data, mutations, initialization)
не содержит ветвлений по конкретной СУБД; всё вендорное — в `mssql/` и `oracle/`.
"""
