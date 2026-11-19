{{ config(enabled=var('parallel_ingestion', false), tags=['parallel', 'parallel_freshness']) }}
SELECT event_id FROM {{ source('inventory_stream', 'inventory_movements') }}
WHERE occurred_at > CURRENT_TIMESTAMP
UNION ALL
SELECT event_id FROM {{ source('shipment_events', 'shipment_updates') }}
WHERE occurred_at > CURRENT_TIMESTAMP
UNION ALL
SELECT event_id FROM {{ source('parallel_ops', 'batches') }}
WHERE loaded_at > CURRENT_TIMESTAMP
