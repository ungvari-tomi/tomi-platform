{{ config(materialized='view') }}
SELECT event_id, dataset_version, seed, interval_seconds, sequence, occurred_at,
       order_id, order_interval_seconds, order_dataset_version, shipment_id, status
FROM {{ source('shipment_events', 'shipment_updates') }}
