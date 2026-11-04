{{ config(enabled=var('check_freshness', false), tags=['freshness']) }}

select o.dataset_id, o.order_id
from {{ source('retailflow_raw', 'orders') }} o
join {{ source('retailflow_ops', 'datasets') }} d using (dataset_id)
where d.dataset_version = '{{ env_var("RETAILFLOW_DATASET_VERSION", "orders-v3") | replace("'", "''") }}'
  and d.seed = {{ env_var('RETAILFLOW_SEED', '42') | int }}
  and d.interval_seconds = {{ env_var('RETAILFLOW_INTERVAL_SECONDS', '600') | int }}
  and o.created_at > current_timestamp + interval '60 seconds'
