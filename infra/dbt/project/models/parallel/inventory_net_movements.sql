{{ config(materialized='view') }}
-- Net movement since ingestion began, not on-hand stock: no opening balance is supplied.
SELECT seed, interval_seconds, product_id, warehouse_id,
       SUM(quantity_delta) AS net_quantity_moved, COUNT(*) AS movement_count,
       MAX(occurred_at) AS latest_movement_at
FROM {{ ref('stg_inventory_movements') }}
GROUP BY seed, interval_seconds, product_id, warehouse_id
