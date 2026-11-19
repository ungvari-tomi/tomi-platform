{{ config(materialized='view') }}
SELECT event_id, dataset_version, seed, interval_seconds, sequence, occurred_at,
       product_id, warehouse_id, quantity_delta, reason
FROM {{ source('inventory_stream', 'inventory_movements') }}
