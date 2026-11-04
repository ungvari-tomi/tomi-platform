"""Scheduled source extraction and transactional raw warehouse loading."""

from datetime import UTC, datetime, timedelta

from airflow.sdk import dag, get_current_context, task

from retailflow.alerts import dag_failure


def requested_rate_date(interval_end: datetime | None, run_after: datetime) -> str:
    """Manual Airflow 3 runs have no interval; use their stable recorded queue time."""
    return (interval_end or run_after).astimezone(UTC).date().isoformat()


@dag(
    dag_id="retailflow_orders",
    schedule="*/10 * * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    on_failure_callback=dag_failure,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(seconds=30),
        "execution_timeout": timedelta(minutes=20),
    },
    tags=["retailflow", "extraction", "csv"],
)
def orders():
    @task
    def extract():
        from retailflow.extraction import scheduled_orders

        context = get_current_context()
        result = scheduled_orders(context["run_id"])
        return {"cursor": result["cursor"], "batches": len(result["manifests"])}

    @task
    def load(extraction_result: dict):
        from retailflow.warehouse import scheduled_load

        return scheduled_load("orders")

    @task
    def dbt_build(load_result: dict):
        from retailflow.transformation import scheduled_build

        context = get_current_context()
        return scheduled_build(context["dag"].dag_id, context["run_id"], context["ti"].try_number)

    dbt_build(load(extract()))


@dag(
    dag_id="retailflow_exchange_rates",
    schedule="0 17 * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    on_failure_callback=dag_failure,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=1),
        "execution_timeout": timedelta(minutes=5),
    },
    tags=["retailflow", "extraction", "json"],
)
def exchange_rates():
    @task(execution_timeout=timedelta(minutes=20))
    def extract():
        from retailflow.extraction import scheduled_all_rates

        context = get_current_context()
        # Use the interval's closing date, not the start of yesterday's interval.
        requested = requested_rate_date(
            context.get("data_interval_end"), context["dag_run"].run_after
        )
        return {"manifests": scheduled_all_rates(context["run_id"], requested)}

    @task
    def load(extraction_result: dict):
        from retailflow.warehouse import scheduled_load

        return scheduled_load("exchange-rates")

    @task
    def dbt_build(load_result: dict):
        from retailflow.transformation import scheduled_build

        context = get_current_context()
        return scheduled_build(context["dag"].dag_id, context["run_id"], context["ti"].try_number)

    dbt_build(load(extract()))


@dag(
    dag_id="retailflow_customers",
    schedule="*/10 * * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    on_failure_callback=dag_failure,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(seconds=30),
        "execution_timeout": timedelta(minutes=20),
    },
    tags=["retailflow", "customers", "pseudonymized"],
)
def customers():
    @task
    def extract():
        from retailflow.customer_scheduling import scheduled_extract

        result = scheduled_extract(get_current_context()["run_id"])
        return {"cursor": result["cursor"], "batches": len(result["manifests"])}

    @task
    def load(extraction_result: dict):
        from retailflow.customer_scheduling import scheduled_load

        return scheduled_load()

    @task
    def dbt_build(load_result: dict):
        from retailflow.transformation import scheduled_build

        context = get_current_context()
        return scheduled_build(
            context["dag"].dag_id, context["run_id"], context["ti"].try_number, customers=True
        )

    dbt_build(load(extract()))


orders()
exchange_rates()
customers()
