{{ config(enabled=var('customer_pipeline', false), tags=['customers']) }}
-- Missing, orphaned or inconsistent snapshots remain visible as failing checks.
SELECT 'missing_or_mismatched' AS issue
FROM {{ ref('customer_orders') }}
WHERE customer_as_of IS NULL OR customer_country != country
   OR synthetic IS DISTINCT FROM TRUE
UNION ALL
SELECT 'orphan_snapshot' AS issue
FROM {{ ref('stg_customers') }} c
WHERE NOT EXISTS (
  SELECT 1 FROM {{ source('customer_demo', 'customer_batches') }} cb
  JOIN {{ source('customer_demo', 'order_batches') }} ob
    ON ob.manifest = cb.manifest AND ob.dataset_id = cb.dataset_id
  JOIN {{ ref('stg_customer_orders') }} o
    ON o.dataset_id = ob.dataset_id AND o.order_id = ob.order_id
   AND o.customer_token = cb.customer_token
  WHERE cb.dataset_id = c.dataset_id AND cb.customer_token = c.customer_token
    AND cb.as_of = c.as_of
)
UNION ALL
SELECT 'order_count_changed' AS issue
WHERE (SELECT COUNT(*) FROM {{ ref('stg_customer_orders') }})
   != (SELECT COUNT(*) FROM {{ ref('customer_orders') }})
