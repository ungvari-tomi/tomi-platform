{{ config(enabled=var('customer_pipeline', false), tags=['customers', 'customer_freshness']) }}
SELECT 'future_order' AS issue FROM {{ source('customer_demo', 'orders') }}
WHERE created_at > CURRENT_TIMESTAMP
UNION ALL
SELECT 'future_load' AS issue FROM {{ source('customer_demo', 'batches') }}
WHERE loaded_at > CURRENT_TIMESTAMP
