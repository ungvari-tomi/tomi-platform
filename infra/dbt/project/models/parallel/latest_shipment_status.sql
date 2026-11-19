{{ config(materialized='view') }}
SELECT *
FROM {{ ref('stg_shipment_updates') }}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY seed, interval_seconds, order_interval_seconds, shipment_id
    ORDER BY occurred_at DESC, sequence DESC, event_id
) = 1
