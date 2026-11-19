{{ config(enabled=var('parallel_ingestion', false), tags=['parallel']) }}
SELECT event_id FROM {{ ref('stg_inventory_movements') }}
WHERE quantity_delta = 0 OR quantity_delta NOT BETWEEN -50 AND 50
   OR (reason = 'sale') != (quantity_delta < 0)
   OR NOT REGEXP_FULL_MATCH(event_id, '[a-f0-9]{64}')
UNION ALL
SELECT event_id FROM {{ ref('stg_shipment_updates') }}
WHERE order_id < 1 OR NOT REGEXP_FULL_MATCH(event_id, '[a-f0-9]{64}')
   OR NOT REGEXP_FULL_MATCH(shipment_id, 'shipment-[a-f0-9]{24}')
UNION ALL
SELECT b.event_id FROM {{ source('parallel_ops', 'batches') }} b
LEFT JOIN {{ source('parallel_ops', 'events') }} e ON e.event_id = b.event_id
WHERE e.event_id IS NULL
   OR (b.dataset_version = 'inventory-v1' AND (
       b.transport != 'kafka' OR b.topic != 'retailflow.inventory.v1'
       OR b.partition_id NOT BETWEEN 0 AND 2 OR b.offset_id < 0))
   OR (b.dataset_version = 'shipments-v1' AND b.transport != 'webhook')
