"""Paused opt-in product extraction, atomic loading and catalog dbt quality checks."""

from datetime import UTC, datetime, timedelta

from airflow.sdk import dag, get_current_context, task

from retailflow.alerts import dag_failure


@dag(
    dag_id="retailflow_catalog",
    schedule="0 */6 * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    on_failure_callback=dag_failure,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=1),
        "execution_timeout": timedelta(minutes=10),
    },
    tags=["retailflow", "catalog", "mongodb"],
)
def catalog():
    @task
    def extract():
        from retailflow.catalog_pipeline import scheduled_extract

        return scheduled_extract(get_current_context()["run_id"])

    @task
    def load(extracted: dict):
        from retailflow.catalog_pipeline import scheduled_load

        return scheduled_load(extracted["manifest"])

    @task
    def dbt_build(loaded: dict):
        from retailflow.transformation import scheduled_build

        context = get_current_context()
        return scheduled_build(
            context["dag"].dag_id, context["run_id"], context["ti"].try_number, catalog=True
        )

    dbt_build(load(extract()))


catalog()
