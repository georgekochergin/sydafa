# sydafa-airflow: Airflow Integration Package for sydafa

Пакет интеграции Apache Airflow с сервисом эмуляции СУБД **sydafa**. Предоставляет операторы и хук для управления жизненным циклом СУБД-источников (Lazy Start, продление TTL, принудительная деактивация) и запуска транзакционных DDL/DML миграций прямо из DAG-ов Airflow.

---

## 1. Установка

### Установка из Git-репозитория (рекомендуется для сторонних проектов)
В `requirements.txt` вашего окружения Airflow (или Dockerfile):
```text
git+https://github.com/georgekochergin/sydafa.git#subdirectory=integrations/airflow
```

### Локальная установка для разработки (editable mode)
```bash
pip install -e /path/to/sydafa/integrations/airflow
```

---

## 2. Настройка подключения к sydafa (Airflow Connection)

Все операторы и хук взаимодействуют с sydafa через стандартный механизм **Airflow Connection**.
Параметр **`conn_id` является строго обязательным** без значений по умолчанию — вы всегда явно указываете целевое подключение (например, `conn_id="sydafa_default"` или любое кастомное имя, например `conn_id="sydafa_staging"`).

Подключение можно объявить любым из трех стандартных способов Airflow:

### Способ 1: Переменная окружения (Environment Variable)
Формат переменной: `AIRFLOW_CONN_<CONN_ID_В_ВЕРХНЕМ_РЕГИСТРЕ>`:
```bash
export AIRFLOW_CONN_SYDAFA_DEFAULT='http://sydafa:8000'
```
В `docker-compose.yaml` (секция `environment` сервисов Airflow):
```yaml
AIRFLOW_CONN_SYDAFA_DEFAULT: "http://sydafa:8000"
```

### Способ 2: Через Airflow CLI
```bash
airflow connections add sydafa_default \
    --conn-type http \
    --conn-host http://sydafa:8000
```

### Способ 3: Через Airflow Web UI
1. Перейдите в меню: **Admin -> Connections**;
2. Нажмите **+ (Add a new record)**;
3. Заполните поля:
   - **Connection Id**: `sydafa_default` (или ваше имя)
   - **Connection Type**: `HTTP`
   - **Host**: `http://sydafa:8000` (или `http://localhost:8000`)
4. Нажмите **Save**.

---

## 3. Операторы

Все операторы поддерживают Airflow Jinja-шаблонизацию (`template_fields`), включая параметр `conn_id`.

### `SydafaActivateOperator`
Явное пробуждение СУБД источника (Lazy Start) и установка/продление времени жизни (`ttl_minutes`) перед запуском выгрузки данных.

Параметры:
- `conn_id` (*str, обязательно*): идентификатор Airflow Connection;
- `source` (*str, обязательно*): имя источника данных в sydafa (`mssql`, `oracle`, `crm` и т.д.);
- `ttl_minutes` (*int, по умолчанию 15*): количество минут активной работы СУБД до автоматической остановки;
- `timeout_seconds` (*int, по умолчанию 180*): сетевой таймаут HTTP-запроса ожидания готовности СУБД.

### `SydafaMigrateOperator`
Транзакционное применение упорядоченного батча DDL и DML операций (All-or-Nothing). Используется для генерации синтетических данных перед тестами или эволюции схемы.

Параметры:
- `conn_id` (*str, обязательно*): идентификатор Airflow Connection;
- `source` (*str, обязательно*): имя источника данных в sydafa;
- `operations` (*list[dict], обязательно*): упорядоченный список DDL/DML операций согласно контракту sydafa (`create_table`, `append`, `update`, `truncate` и т.д.);
- `timeout_seconds` (*int, по умолчанию 300*): сетевой таймаут HTTP-запроса.

### `SydafaDeactivateOperator`
Принудительная немедленная остановка контейнера СУБД источника для освобождения оперативной памяти хоста (0 MB RAM).

Параметры:
- `conn_id` (*str, обязательно*): идентификатор Airflow Connection;
- `source` (*str, обязательно*): имя источника данных в sydafa;
- `timeout_seconds` (*int, по умолчанию 60*): сетевой таймаут HTTP-запроса.

---

## 4. Пример Airflow DAG

```python
from datetime import datetime
from airflow import DAG
from airflow.operators.bash import BashOperator

from sydafa_airflow.operators import (
    SydafaActivateOperator,
    SydafaMigrateOperator,
    SydafaDeactivateOperator,
)

with DAG(
    dag_id="example_sydafa_pipeline",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
) as dag:

    # 1. Пробуждение источника СУБД перед выполнением пайплайна
    activate_mssql = SydafaActivateOperator(
        task_id="activate_mssql",
        conn_id="sydafa_default",
        source="mssql",
        ttl_minutes=20,
    )

    # 2. Опционально: наполнение свежими данными через migrate
    generate_test_data = SydafaMigrateOperator(
        task_id="generate_test_data",
        conn_id="sydafa_default",
        source="mssql",
        operations=[
            {"op": "append", "entity_fqn": "demo.orders", "rows": 100},
        ],
    )

    # 3. Основная джоба выгрузки (например, Spark / Trino / dbt)
    extract_load = BashOperator(
        task_id="extract_load",
        bash_command="echo 'Чтение данных из СУБД по JDBC...'",
    )

    # 4. Освобождение RAM после завершения выгрузки
    deactivate_mssql = SydafaDeactivateOperator(
        task_id="deactivate_mssql",
        conn_id="sydafa_default",
        source="mssql",
    )

    activate_mssql >> generate_test_data >> extract_load >> deactivate_mssql
```
