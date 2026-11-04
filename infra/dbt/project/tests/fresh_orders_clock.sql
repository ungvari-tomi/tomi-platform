{{ config(enabled=var('check_freshness', false), tags=['freshness']) }}

SELECT o.dataset_id, o.order_id
FROM {{ source('retailflow_raw', 'orders') }} o
JOIN {{ source('retailflow_ops', 'datasets') }} d USING (dataset_id)
WHERE d.dataset_version = '{{ env_var("RETAILFLOW_DATASET_VERSION", "orders-v3") | replace("'", "''") }}'
  AND d.seed = {{ env_var('RETAILFLOW_SEED', '42') | int }}
  AND d.interval_seconds = {{ env_var('RETAILFLOW_INTERVAL_SECONDS', '600') | int }}
  AND o.created_at > CURRENT_TIMESTAMP + INTERVAL '60 seconds'
