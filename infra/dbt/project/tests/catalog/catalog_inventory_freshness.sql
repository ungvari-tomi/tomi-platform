{{ config(enabled=var('catalog_reporting', false), tags=['catalog_report_freshness']) }}

SELECT 'missing_stale_or_future_inventory' AS failure
WHERE NOT EXISTS (
    SELECT 1 FROM {{ source('inventory_stream', 'inventory_movements') }}
    WHERE seed = {{ env_var('RETAILFLOW_SEED', '42') | int }}
        AND interval_seconds = {{ env_var('RETAILFLOW_EVENT_INTERVAL_SECONDS', '600') | int }}
)
OR EXISTS (
    SELECT 1 FROM {{ source('inventory_stream', 'inventory_movements') }}
    WHERE seed = {{ env_var('RETAILFLOW_SEED', '42') | int }}
        AND interval_seconds = {{ env_var('RETAILFLOW_EVENT_INTERVAL_SECONDS', '600') | int }}
    GROUP BY seed, interval_seconds
    HAVING MAX(occurred_at) < CURRENT_TIMESTAMP - INTERVAL '30 minutes'
        OR MAX(occurred_at) > CURRENT_TIMESTAMP
)
